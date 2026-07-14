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

"""Shared NICo API client for NICo validation scripts.

Handles authentication, authenticated GET requests with pagination, and proper
URL encoding.
"""

import base64
import json
import os
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_PAGE_SIZE = 100
DEFAULT_API_NAME = "carbide"
OIDC_TOKEN_TIMEOUT_SECONDS = 30


class NicoAuthError(RuntimeError):
    """Raised when NICo authentication cannot be resolved."""


class NicoAuth(NamedTuple):
    """Resolved NICo bearer credential with a non-secret source label."""

    token: str
    source: str


def _env(name: str) -> str:
    """Return a stripped environment value or an empty string."""
    return os.environ.get(name, "").strip()


def nico_api_name() -> str:
    """Return the NICo API path segment (``carbide`` or ``nico``)."""
    return _env("NICO_API_NAME") or DEFAULT_API_NAME


def resolve_auth(*, timeout: int = OIDC_TOKEN_TIMEOUT_SECONDS) -> NicoAuth:
    """Resolve NICo API authentication from environment variables.

    Resolution order:
    1. ``NICO_BEARER_TOKEN`` for locally supplied tokens.
    2. OIDC client credentials using ``NICO_SSA_ISSUER``,
       ``NICO_CLIENT_ID``, and ``NICO_CLIENT_SECRET``.

    ``NGC_API_KEY`` is intentionally not used for NICo authentication.
    """
    bearer_token = _env("NICO_BEARER_TOKEN")
    if bearer_token:
        return NicoAuth(token=bearer_token, source="NICO_BEARER_TOKEN")

    issuer_url = _env("NICO_SSA_ISSUER")
    client_id = _env("NICO_CLIENT_ID")
    client_secret = _env("NICO_CLIENT_SECRET")
    scope = _env("NICO_OIDC_SCOPE")

    missing = [
        name
        for name, value in (
            ("NICO_SSA_ISSUER", issuer_url),
            ("NICO_CLIENT_ID", client_id),
            ("NICO_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise NicoAuthError(
            "NICo authentication is not configured; set NICO_BEARER_TOKEN or configure "
            f"OIDC client credentials (missing: {', '.join(missing)})"
        )

    return NicoAuth(
        token=_request_oidc_token(
            issuer_url=issuer_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            timeout=timeout,
        ),
        source="oidc_client_credentials",
    )


def _request_oidc_token(
    *,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    scope: str = "",
    timeout: int = OIDC_TOKEN_TIMEOUT_SECONDS,
) -> str:
    """Request an access token using OIDC client_credentials."""
    token_url = _discover_oidc_token_endpoint(issuer_url=issuer_url, timeout=timeout)
    form = {"grant_type": "client_credentials"}
    if scope:
        form["scope"] = scope

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = Request(
        token_url,
        data=urlencode(form).encode(),
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as e:
        body = ""
        if e.fp:
            body = e.fp.read().decode(errors="replace")[:300]
        detail = f"HTTP {e.code}"
        if body:
            detail = f"{detail}: {body}"
        raise NicoAuthError(f"OIDC token request failed ({detail})") from e
    except URLError as e:
        raise NicoAuthError(f"OIDC token request failed: {e.reason}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise NicoAuthError("OIDC token response was not valid JSON") from e

    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise NicoAuthError("OIDC token response did not contain access_token")
    return token.strip()


def _discover_oidc_token_endpoint(*, issuer_url: str, timeout: int = OIDC_TOKEN_TIMEOUT_SECONDS) -> str:
    """Resolve the OIDC token endpoint from issuer metadata."""
    discovery_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    request = Request(discovery_url)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as e:
        body = ""
        if e.fp:
            body = e.fp.read().decode(errors="replace")[:300]
        detail = f"HTTP {e.code}"
        if body:
            detail = f"{detail}: {body}"
        raise NicoAuthError(f"OIDC discovery failed ({detail})") from e
    except URLError as e:
        raise NicoAuthError(f"OIDC discovery failed: {e.reason}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise NicoAuthError("OIDC discovery response was not valid JSON") from e

    token_endpoint = payload.get("token_endpoint")
    if not isinstance(token_endpoint, str) or not token_endpoint.strip():
        raise NicoAuthError("OIDC discovery response did not contain token_endpoint")
    return token_endpoint.strip()


def forge_get(
    org: str,
    path: str,
    token: str,
    *,
    base_url: str,
    params: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Make an authenticated GET request to a single NICo API page.

    Args:
        org: NGC org name.
        path: API path relative to the API name segment (e.g., "machine", "expected-machine").
        token: Bearer token.
        base_url: NICo API base URL.
        params: Query parameters (will be URL-encoded).
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        HTTPError: On non-2xx response.
    """
    url = f"{base_url}/{org}/{nico_api_name()}/{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = ""
        if e.fp:
            body = e.fp.read().decode(errors="replace")[:500]
        raise type(e)(e.url, e.code, f"{e.reason}: {body}", e.headers, None) from e


def forge_get_all(
    org: str,
    path: str,
    token: str,
    *,
    base_url: str,
    params: dict[str, str] | None = None,
    result_key: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Fetch all pages from a paginated NICo API endpoint.

    Args:
        org: NGC org name.
        path: API path relative to the API name segment (e.g., "machine", "expected-machine").
        token: Bearer token.
        base_url: NICo API base URL.
        params: Additional query parameters.
        result_key: JSON key containing the results array. If None, the response
            itself is expected to be a list, or auto-detected from common keys.
        page_size: Number of items per page (max 100).
        timeout: Per-request timeout in seconds.

    Returns:
        Combined list of all items across all pages.
    """
    all_items: list[dict[str, Any]] = []
    page_number = 1
    # The API caps page size at 100; compare against the effective size (not the
    # caller's raw page_size) so a request for >100 doesn't stop after one page.
    effective_page_size = min(page_size, 100)

    while True:
        page_params = dict(params or {})
        page_params["pageSize"] = str(effective_page_size)
        page_params["pageNumber"] = str(page_number)

        resp = forge_get(org, path, token, base_url=base_url, params=page_params, timeout=timeout)

        # The NICo API is not uniform: some endpoints return a bare JSON list,
        # others wrap the array under result_key (or another well-known key).
        if isinstance(resp, list):
            items = resp
        elif result_key and result_key in resp:
            items = resp[result_key]
        else:
            # Auto-detect: look for common NICo API result keys
            for key in ("machines", "expectedMachines", "instances", "sites"):
                if key in resp:
                    items = resp[key]
                    break
            else:
                # Response is a single object, not a list
                items = [resp] if resp else []

        all_items.extend(items)

        # Check if there are more pages
        if len(items) < effective_page_size:
            break

        page_number += 1

    return all_items


def classify_health(health: dict[str, Any]) -> str:
    """Classify machine health as 'healthy' or 'unhealthy'."""
    alerts = health.get("alerts", [])
    return "unhealthy" if alerts else "healthy"


def probe_text(probe: dict[str, Any]) -> str:
    """Return the lowercased ``id`` + ``target`` + ``message`` text for matching.

    NICo reports BMC sensors under a single ``BmcSensor`` probe id and carries
    the sensor identity in ``target`` and the entity type in ``message`` (e.g.
    ``power_supply``, ``temperature``), so all three fields are searched.
    """
    parts = [probe.get("id"), probe.get("target"), probe.get("message")]
    return " ".join(p for p in parts if isinstance(p, str)).lower()


def sum_capabilities(capabilities: list[dict[str, Any]], cap_type: str) -> int:
    """Sum the count field for capabilities of a given type.

    Per the OpenAPI spec, MachineCapability.count is the device count
    (e.g., count=2 means 2 DPUs). We sum across all entries of the type.
    count is nullable (*int); a missing or null count is treated as 1.
    """
    total = 0
    for c in capabilities:
        if c.get("type") != cap_type:
            continue
        count = c.get("count")
        total += count if isinstance(count, int) else 1
    return total
