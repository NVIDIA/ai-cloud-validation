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

"""Stateful fake of the Firebird API service-account surface, for the harness.

Models what the scripts rely on: ``POST /service-accounts`` mints an account
with one client secret; ``/auth/token`` issues ``tok:<account>`` only for a
live secret; rotation replaces the secret (or keeps the old one valid, as a
rotation grace period would); project roles gate ``GET /projects/{p}`` for
account tokens. The run's own token (``test-token``) is the tenant admin.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from .harness import PROJECT, HttpError, Route, WithToken

TENANT = "tenant.T"
ADMIN_TOKEN = "test-token"


class FakeIam:
    """Service accounts, their secrets and project roles, and token issuance."""

    def __init__(self, *, keep_rotated_secrets: bool = False, admin_me: dict[str, Any] | None = None) -> None:
        """Start empty; ``keep_rotated_secrets`` models a rotation grace period."""
        self.keep_rotated_secrets = keep_rotated_secrets
        self.admin_me = admin_me or {"userId": "service-account.admin"}
        self.accounts: dict[str, dict[str, Any]] = {}
        self.roles: dict[tuple[str, str], list[str]] = {}
        self.deleted: list[str] = []
        self.token_requests: list[str] = []  # secret presented at each /auth/token call
        self._ids = itertools.count(1)
        self._secrets = itertools.count(1)

    def account_for(self, token: str | None) -> str | None:
        """Return the account a ``tok:<id>`` token belongs to (None for the admin token)."""
        return token.removeprefix("tok:") if token and token.startswith("tok:") else None

    def routes(self, *projects: str, accounts: int = 3) -> dict[str, Route]:
        """Return routes for up to ``accounts`` accounts, and project reads for ``projects``."""
        routes: dict[str, Route] = {
            "POST /service-accounts": self._create,
            "POST /auth/token": self._token,
            "GET /users/me": WithToken(self._me),
        }
        for project in (PROJECT, *projects):
            routes[f"GET /projects/{project}"] = WithToken(self._project(project))
        for n in range(1, accounts + 1):
            sa_id = f"service-account.{n}"
            routes[f"GET /service-accounts/{sa_id}"] = self._get(sa_id)
            routes[f"DELETE /service-accounts/{sa_id}"] = self._delete(sa_id)
            routes[f"POST /service-accounts/{sa_id}/secret"] = self._rotate(sa_id)
            for project in (PROJECT, *projects):
                routes[f"PUT /projects/{project}/service-accounts/{sa_id}/roles"] = self._set_roles(sa_id, project)
        return routes

    def _new_secret(self) -> str:
        """Return a fresh client secret."""
        return f"secret-{next(self._secrets)}"

    def _create(self, body: Any, _q: Any) -> dict[str, Any]:
        """POST /service-accounts: mint an account with one secret."""
        sa_id = f"service-account.{next(self._ids)}"
        secret = self._new_secret()
        name = (body or {}).get("displayName", "")
        self.accounts[sa_id] = {"name": name, "client_id": f"client-{sa_id}", "secrets": [secret]}
        return {
            "serviceAccount": {"id": sa_id, "displayName": name, "tenantId": TENANT, "clientId": f"client-{sa_id}"},
            "credentials": {"clientId": f"client-{sa_id}", "clientSecret": secret},
        }

    def _token(self, body: Any, _q: Any) -> dict[str, Any]:
        """POST /auth/token: issue a token for a live secret, else 401."""
        client_id, secret = (body or {}).get("clientId"), (body or {}).get("clientSecret")
        self.token_requests.append(secret)
        for sa_id, account in self.accounts.items():
            if account["client_id"] == client_id and secret in account["secrets"]:
                return {"accessToken": f"tok:{sa_id}", "tokenType": "Bearer", "expiresIn": 300}
        raise HttpError(401)

    def _me(self, _b: Any, _q: Any, token: str | None) -> dict[str, Any]:
        """GET /users/me: the admin, or the account the token names."""
        sa_id = self.account_for(token)
        if sa_id is None:
            return self.admin_me
        if sa_id not in self.accounts:
            raise HttpError(401)
        return {"userId": sa_id, "displayName": self.accounts[sa_id]["name"]}

    def _project(self, project: str) -> Callable[[Any, Any, str | None], dict[str, Any]]:
        """GET /projects/{p}: 403 for an account without a role there."""

        def get(_b: Any, _q: Any, token: str | None) -> dict[str, Any]:
            sa_id = self.account_for(token)
            if sa_id is not None and not self.roles.get((sa_id, project)):
                raise HttpError(403)
            return {"project": {"id": project, "tenantId": TENANT, "name": project}}

        return get

    def _get(self, sa_id: str) -> Route:
        """GET /service-accounts/{id}: the flat ServiceAccountResponse, 404 once gone."""

        def get(_b: Any, _q: Any) -> dict[str, Any]:
            if sa_id not in self.accounts:
                raise HttpError(404)
            account = self.accounts[sa_id]
            return {"id": sa_id, "displayName": account["name"], "tenantId": TENANT, "clientId": account["client_id"]}

        return get

    def _delete(self, sa_id: str) -> Route:
        """DELETE /service-accounts/{id}: 404 once gone."""

        def delete(_b: Any, _q: Any) -> dict[str, Any]:
            if sa_id not in self.accounts:
                raise HttpError(404)
            del self.accounts[sa_id]
            self.deleted.append(sa_id)
            return {}

        return delete

    def _rotate(self, sa_id: str) -> Route:
        """POST /service-accounts/{id}/secret: replace (or add) a secret."""

        def rotate(_b: Any, _q: Any) -> dict[str, Any]:
            account = self.accounts[sa_id]
            secret = self._new_secret()
            account["secrets"] = [*account["secrets"], secret] if self.keep_rotated_secrets else [secret]
            return {"credentials": {"clientId": account["client_id"], "clientSecret": secret}}

        return rotate

    def _set_roles(self, sa_id: str, project: str) -> Route:
        """PUT /projects/{p}/service-accounts/{id}/roles: replace the roles."""

        def put(body: Any, _q: Any) -> dict[str, Any]:
            self.roles[(sa_id, project)] = list((body or {}).get("roles") or [])
            return {"subjectId": sa_id, "projectId": project, "roles": self.roles[(sa_id, project)]}

        return put
