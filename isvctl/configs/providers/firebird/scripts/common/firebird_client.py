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

"""Minimal Firebird BMaaS REST API client.

Stdlib only. Authenticates with service-account client credentials (or a
pre-minted bearer token), issues JSON requests under ``/api/v1``, and waits on
the asynchronous Operations that BM mutations return.

Environment:
    FIREBIRD_API_BASE       API base URL (default: https://dgxc.firebird.ai)
    FIREBIRD_PROJECT_ID     Project the BM is attached to (project.ULID) - required
    FIREBIRD_CLIENT_ID      Service-account client ID     } or FIREBIRD_BEARER_TOKEN
    FIREBIRD_CLIENT_SECRET  Service-account client secret }
    FIREBIRD_TENANT_ID      Optional tenant.ULID when the account belongs to several tenants
"""

import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_API_BASE = "https://dgxc.firebird.ai"
REQUEST_TIMEOUT_SECONDS = 60
# Sent on every request. Python's default ("Python-urllib/3.x") is refused with
# 403 by some edge bot filters in front of the identity provider, so name the client.
USER_AGENT = "ai-cloud-validation-firebird/1.0"
OPERATION_TERMINAL = ("COMPLETED", "FAILED")
# HTTP statuses the API answers for a route whose optional service (such as the
# NCP report or IB partitions) is not enabled on this deployment: 404 for the
# unknown route, or 501 (Not Implemented).
NOT_REGISTERED_STATUSES = (404, 501)


class FirebirdApiError(RuntimeError):
    """Raised for a non-2xx API response or a failed Operation."""

    def __init__(self, message: str, status: int = 0) -> None:
        """Store the HTTP status (0 when not an HTTP error)."""
        super().__init__(message)
        self.status = status


def _env(name: str) -> str:
    """Return a stripped environment value or an empty string."""
    return os.environ.get(name, "").strip()


def log(message: str) -> None:
    """Print progress to stderr (stdout is reserved for the JSON result)."""
    print(message, file=sys.stderr, flush=True)


def remaining(deadline: float) -> int:
    """Return whole seconds left before ``deadline`` (a ``time.monotonic()`` value), at least 1."""
    return max(1, int(deadline - time.monotonic()))


def is_not_registered(error: FirebirdApiError) -> bool:
    """Return whether ``error`` means the requested API is not served here.

    Only meaningful for a route whose path names no resource that could itself
    be missing (or only ones already known to exist, such as the client's
    project), so a 404 cannot mean "that resource does not exist".
    """
    return error.status in NOT_REGISTERED_STATUSES


def rfc3339(ts: datetime) -> str:
    """Format a UTC datetime as RFC3339 with a Z suffix (the API's query format)."""
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a protobuf JSON timestamp ("...Z", UTC, up to ns fraction) to whole seconds.

    Returns None for an unset (null or empty) timestamp.
    """
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        return datetime.fromisoformat(value[:19]).replace(tzinfo=UTC)
    except ValueError:
        return None


def to_state(bm: dict[str, Any]) -> str:
    """Map a BM lifecycle state to the suite's lowercase vocabulary.

    RUNNING -> "running", STOPPED -> "stopped"; other states pass through
    lowercased (e.g. "provisioning", "available", "degraded").
    """
    return str(bm.get("state", "")).lower()


class FirebirdClient:
    """Authenticated client for one project on the Firebird BMaaS API."""

    def __init__(self, client_id: str = "", client_secret: str = "") -> None:
        """Resolve endpoint, project, and credentials from the environment.

        ``client_id``/``client_secret`` authenticate as that service account
        instead of the run's own credentials (``FIREBIRD_*`` auth variables are
        then ignored); endpoint, project, and tenant still come from the environment.
        """
        self.base_url = (_env("FIREBIRD_API_BASE") or DEFAULT_API_BASE).rstrip("/")
        self.project_id = _env("FIREBIRD_PROJECT_ID")
        if not self.project_id:
            raise FirebirdApiError("FIREBIRD_PROJECT_ID is not set")
        self._tenant_id = _env("FIREBIRD_TENANT_ID")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = "" if client_id else _env("FIREBIRD_BEARER_TOKEN")
        self._token_expires_at = float("inf") if self._token else 0.0

    # ── Auth ──────────────────────────────────────────────────────────

    def access_token(self) -> str:
        """Return a valid bearer token, fetching or refreshing it as needed."""
        if self._token and time.monotonic() < self._token_expires_at - 30:
            return self._token
        self.authenticate()
        return self._token

    def authenticate(self) -> dict[str, Any]:
        """Fetch a fresh token with the client's credentials, cache it, and return the token response."""
        client_id = self._client_id or _env("FIREBIRD_CLIENT_ID")
        client_secret = self._client_secret or _env("FIREBIRD_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise FirebirdApiError(
                "Firebird auth is not configured; set FIREBIRD_CLIENT_ID and FIREBIRD_CLIENT_SECRET "
                "(or FIREBIRD_BEARER_TOKEN)"
            )
        payload = self.issue_token(client_id, client_secret)
        self._token = payload["accessToken"]
        self._token_expires_at = time.monotonic() + int(payload.get("expiresIn") or 300)
        return payload

    def issue_token(self, client_id: str, client_secret: str) -> dict[str, Any]:
        """Exchange service-account credentials for a token (``client_credentials``).

        Raises FirebirdApiError with the HTTP status when the credentials are refused.
        """
        body = {"grantType": "client_credentials", "clientId": client_id, "clientSecret": client_secret}
        payload = self._send("POST", "/api/v1/auth/token", body, token=None)
        token = payload.get("accessToken")
        if not isinstance(token, str) or not token:
            raise FirebirdApiError("token response did not contain accessToken")
        return payload

    # ── Transport ─────────────────────────────────────────────────────

    def _send(
        self, method: str, path: str, body: dict[str, Any] | bytes | None, *, token: str | None
    ) -> dict[str, Any]:
        """Send one request and return the decoded JSON response body.

        A dict body is sent as JSON; ``bytes`` are sent raw (image upload parts).
        """
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if isinstance(body, bytes):
            data = body
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
            if self._tenant_id:
                headers["X-Firebird-Tenant"] = self._tenant_id

        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read().decode()
        except HTTPError as e:
            detail = ""
            if e.fp:
                raw_err = e.fp.read().decode(errors="replace")
                try:
                    parsed = json.loads(raw_err)
                    detail = parsed.get("message", "") or parsed.get("error", "") or raw_err[:300]
                except (json.JSONDecodeError, AttributeError):
                    detail = raw_err[:300]
            raise FirebirdApiError(f"{method} {path}: HTTP {e.code} {detail}".strip(), status=e.code) from e
        except URLError as e:
            raise FirebirdApiError(f"{method} {path}: {e.reason}") from e
        return json.loads(raw) if raw.strip() else {}

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | bytes | None = None,
        params: dict[str, Any] | None = None,
        *,
        prefix: str = "/api/v1",
        bearer: str | None = None,
    ) -> dict[str, Any]:
        """Send an authenticated request to ``<prefix><path>``.

        ``prefix`` is "" for the flat routes served outside ``/api/v1`` (``/topology/*``).
        ``bearer`` sends that token instead of the client's own (token-validation probes).
        """
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        full_path = f"{prefix}{path}" + (f"?{urlencode(query)}" if query else "")
        return self._send(method, full_path, body, token=bearer if bearer is not None else self.access_token())

    def paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Collect every item across ``nextPageToken`` pages.

        Raises ``FirebirdApiError`` when a page repeats an earlier token, which
        would otherwise loop forever.
        """
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_token = ""
        while True:
            page = self.request("GET", path, params={**(params or {}), "pageSize": 100, "pageToken": page_token})
            items.extend(page.get(key) or [])
            page_token = page.get("nextPageToken") or ""
            if not page_token:
                return items
            if page_token in seen:
                raise FirebirdApiError(f"GET {path}: repeated page token {page_token!r}")
            seen.add(page_token)

    # ── Path helpers ──────────────────────────────────────────────────

    def project_path(self, suffix: str = "") -> str:
        """Return a path under the client's project (``suffix`` starts with "/")."""
        return f"/projects/{quote(self.project_id)}{suffix}"

    def get_project(self) -> dict[str, Any]:
        """Return the client's project (its ``tenantId`` is the tenant the run acts in)."""
        project = self.request("GET", self.project_path()).get("project") or {}
        if not project.get("tenantId"):
            raise FirebirdApiError(f"project {self.project_id} response carries no tenantId")
        return project

    # ── BM helpers ────────────────────────────────────────────────────

    def bm_path(self, bm_id: str, suffix: str = "") -> str:
        """Return the project-scoped path for a BM (optionally an action suffix)."""
        return self.project_path(f"/compute/bms/{quote(bm_id)}{suffix}")

    def get_bm(self, bm_id: str) -> dict[str, Any]:
        """Return the BM resource."""
        return self.request("GET", self.bm_path(bm_id)).get("bm") or {}

    def wait_operation(self, operation: dict[str, Any], timeout: int, interval: float | None = None) -> dict[str, Any]:
        """Poll an Operation until COMPLETED; raise if it FAILED or timed out.

        Polls with exponential backoff (2s up to 15s), or every ``interval`` seconds
        when given - for callers that time the Operation and need a fine resolution.
        An Operation already terminal in the response is judged without polling;
        a response that carried no Operation ID to poll raises at once.
        """
        op_id = operation.get("id", "")
        status = operation.get("status", "")
        if not op_id and status not in OPERATION_TERMINAL:
            raise FirebirdApiError("response carried no Operation to wait on")
        deadline = time.monotonic() + timeout
        delay: float = interval or 2
        while status not in OPERATION_TERMINAL:
            if time.monotonic() > deadline:
                raise FirebirdApiError(f"operation {op_id} still {status} after {timeout}s")
            time.sleep(delay)
            delay = interval or min(delay * 2, 15)
            operation = self.request("GET", f"/operations/{quote(op_id)}").get("operation") or {}
            status = operation.get("status", "")
        if status == "FAILED":
            err = operation.get("error") or {}
            action = operation.get("action", "")
            raise FirebirdApiError(
                f"operation {op_id} ({action}) failed: {err.get('code', '')} {err.get('message', '')}"
            )
        return operation

    def bm_action(self, bm_id: str, action: str, body: dict[str, Any] | None = None, timeout: int = 1800) -> None:
        """POST a BM action (``power-on``, ``reboot``, ``provision``, ...) and wait for its Operation."""
        log(f"  {action} {bm_id}")
        response = self.request("POST", self.bm_path(bm_id, f"/{action}"), body or {})
        self.wait_operation(response.get("operation") or {}, timeout)

    def wait_bm(
        self,
        bm_id: str,
        states: tuple[str, ...],
        timeout: int,
        *,
        power: str | None = None,
        need_ip: bool = False,
        subnet: str | None = None,
    ) -> dict[str, Any]:
        """Poll the BM until its state is in ``states`` (and power/IP/subnet match); return it.

        ``subnet`` waits for ``subnetId`` to equal that value ("" = detached), so a
        dependent action never races the readback of a completed attach/detach.
        """
        deadline = time.monotonic() + timeout
        while True:
            bm = self.get_bm(bm_id)
            ok = bm.get("state") in states
            ok = ok and (power is None or bm.get("powerState") == power)
            ok = ok and (not need_ip or bool(bm.get("ipAddress")))
            ok = ok and (subnet is None or (bm.get("subnetId") or "") == subnet)
            if ok:
                return bm
            if bm.get("state") == "DEGRADED" and "DEGRADED" not in states:
                raise FirebirdApiError(f"BM {bm_id} is DEGRADED")
            if time.monotonic() > deadline:
                observed = f"{bm.get('state')}/{bm.get('powerState')}"
                raise FirebirdApiError(f"BM {bm_id} is {observed} after {timeout}s, expected {'|'.join(states)}")
            log(f"  waiting for {bm_id}: {bm.get('state')}/{bm.get('powerState')}")
            time.sleep(15)
