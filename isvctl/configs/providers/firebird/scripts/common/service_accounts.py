# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Service-account (and throwaway-project) helpers for the control-plane, IAM, and security scripts.

A Firebird service account is an OAuth 2.0 confidential client of the tenant's
OIDC issuer: ``POST /service-accounts`` (tenant ADMIN) returns its ID and a client ID +
secret once; ``/auth/token`` exchanges them for a JWT (``client_credentials``).
A new account holds no roles; ``PUT /projects/{p}/service-accounts/{id}/roles``
grants predefined project roles (ADMIN, EDITOR, VIEWER).

Scripts record a created account's ID in their output before waiting on
anything, so the teardown step can delete it even when the creating step fails.
"""

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from common.firebird_client import FirebirdApiError, FirebirdClient, log

SA_PATH = "/service-accounts"
# /auth/token answers refused credentials with 401 (invalid_client) or 400 (an
# OAuth error with a description). Anything else is not a refusal.
REFUSED_STATUSES = (400, 401)
# The least-privileged predefined role: read-only on one project.
MINIMAL_PROJECT_ROLE = "VIEWER"
# The exact names this provider's scripts create (``unique_name``). Deletes and
# the leftover sweep act only on resources named this way.
SA_NAME = re.compile(r"^(isv-cp|isv-iam|isv-sec-sa|isv-sec-lp|isv-sec-audit)-[0-9a-f]{6}$")
PROJECT_NAME = re.compile(r"^isv-lp-[0-9a-f]{6}$")


class RefusedDeleteError(RuntimeError):
    """A delete aimed at a resource this provider did not create; nothing was deleted."""


@dataclass(frozen=True)
class ServiceAccount:
    """A service account this run created, with the one-time credentials."""

    id: str
    name: str
    client_id: str
    client_secret: str


def unique_name(prefix: str) -> str:
    """Return ``prefix`` plus a short random suffix (display names: ``[A-Za-z0-9_-]``)."""
    return f"{prefix}-{secrets.token_hex(3)}"


def create(client: FirebirdClient, prefix: str) -> ServiceAccount:
    """Create a service account named ``<prefix>-<hex>`` and return it with its credentials."""
    name = unique_name(prefix)
    response = client.request("POST", SA_PATH, {"displayName": name})
    account = response.get("serviceAccount") or {}
    credentials = response.get("credentials") or {}
    sa_id = account.get("id") or ""
    if not sa_id:
        raise RuntimeError(f"POST {SA_PATH} returned no service account ID")
    client_id = credentials.get("clientId") or account.get("clientId") or ""
    client_secret = credentials.get("clientSecret") or ""
    log(f"  created service account {sa_id} ({name})")
    return ServiceAccount(sa_id, name, client_id, client_secret)


def require_credentials(account: ServiceAccount) -> None:
    """Raise unless the create response carried the client ID and secret."""
    if not account.client_id or not account.client_secret:
        raise RuntimeError(f"service account {account.id} was created without client credentials")


def _read(client: FirebirdClient, path: str) -> dict[str, Any] | None:
    """GET ``path``; return None when the resource is already gone (404)."""
    try:
        return client.request("GET", path)
    except FirebirdApiError as e:
        if e.status == 404:
            return None
        raise


def delete(client: FirebirdClient, sa_id: str) -> bool:
    """Delete one of this provider's service accounts; return False if it was already gone (404).

    The account is read first, and one whose display name does not match
    ``SA_NAME`` raises ``RefusedDeleteError`` without a DELETE, so a wrong ID
    can never remove the run's own or another admin's account.
    """
    path = f"{SA_PATH}/{quote(sa_id)}"
    response = _read(client, path)
    if response is None:
        return False
    account = response.get("serviceAccount") or response
    name = str(account.get("displayName", ""))
    if not SA_NAME.match(name):
        raise RefusedDeleteError(f"refusing to delete service account {sa_id} ({name!r}): not named by this provider")
    try:
        client.request("DELETE", path)
    except FirebirdApiError as e:
        if e.status == 404:
            return False
        raise
    return True


def delete_project(client: FirebirdClient, project_id: str) -> None:
    """Delete a project a run created; an already-deleted one (404) is fine.

    Refuses (``RefusedDeleteError``, no DELETE) the run's own project and any
    project whose name does not match ``PROJECT_NAME``, read first.
    """
    if project_id == client.project_id:
        raise RefusedDeleteError(f"refusing to delete project {project_id}: it is the run's own project")
    path = f"/projects/{quote(project_id)}"
    response = _read(client, path)
    if response is None:
        return
    name = str((response.get("project") or {}).get("name", ""))
    if not PROJECT_NAME.match(name):
        raise RefusedDeleteError(f"refusing to delete project {project_id} ({name!r}): not named by this provider")
    try:
        client.request("DELETE", path)
    except FirebirdApiError as e:
        if e.status != 404:
            raise


def rotate_secret(client: FirebirdClient, sa_id: str) -> str:
    """Regenerate the account's client secret (``POST .../secret``); return the new one."""
    response = client.request("POST", f"{SA_PATH}/{quote(sa_id)}/secret", {})
    secret = (response.get("credentials") or {}).get("clientSecret") or ""
    if not secret:
        raise RuntimeError(f"secret rotation for {sa_id} returned no new secret")
    return secret


def set_project_roles(client: FirebirdClient, project_id: str, sa_id: str, roles: list[str]) -> list[str]:
    """Replace the account's roles on ``project_id``; return the roles the API reports."""
    path = f"/projects/{quote(project_id)}{SA_PATH}/{quote(sa_id)}/roles"
    response = client.request("PUT", path, {"roles": roles})
    return list(response.get("roles") or [])


def token_refused(client: FirebirdClient, client_id: str, client_secret: str) -> tuple[bool, str]:
    """Try the credentials at ``/auth/token``; return (refused, evidence).

    Raises for a response that neither issues a token nor refuses the
    credentials (5xx, transport), which proves nothing either way.
    """
    try:
        client.issue_token(client_id, client_secret)
    except FirebirdApiError as e:
        if e.status in REFUSED_STATUSES:
            return True, f"HTTP {e.status}"
        raise
    return False, "token issued"


def login(
    client_id: str, client_secret: str, attempts: int = 5, interval: float = 5
) -> tuple[FirebirdClient, dict[str, Any]]:
    """Authenticate as a service account; return its client and the token response.

    Retries a refusal a few times: a just-created service account can take a
    moment to accept its credentials.
    """
    sa_client = FirebirdClient(client_id, client_secret)
    for attempt in range(1, attempts + 1):
        try:
            return sa_client, sa_client.authenticate()
        except FirebirdApiError as e:
            if e.status not in REFUSED_STATUSES or attempt == attempts:
                raise
            log(f"  token for {client_id} refused ({e.status}); retrying in {interval}s")
            time.sleep(interval)
    raise AssertionError("unreachable")


def status_of(call: Callable[[], object]) -> int:
    """Run an API call and return its HTTP status (200 on success).

    For authorization probes: a transport error (status 0) is re-raised,
    because it proves nothing about the decision.
    """
    try:
        call()
    except FirebirdApiError as e:
        if not e.status:
            raise
        return e.status
    return 200
