#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""I/O adapters for the GCP SEC02 short-lived credential probes."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from common.errors import is_transport_disconnect
from google.api_core import exceptions as gax

IAM_API_BASE = "https://iam.googleapis.com/v1"
STS_TOKEN_ENDPOINT = "https://sts.googleapis.com/v1/token"
TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"

IAM_PROPAGATION_TIMEOUT_SECONDS = 180.0
IAM_PROPAGATION_POLL_SECONDS = 15.0
WIF_OPERATION_TIMEOUT_SECONDS = 90.0
WIF_OPERATION_POLL_SECONDS = 2.0
WIF_REQUEST_ATTEMPTS = 5
WIF_REQUEST_BACKOFF_SECONDS = 2.0
WIF_POOL_DESCRIPTION_PREFIX = "Temporary SEC02 fixture (CreatedBy=isvtest; Invocation="
_WIF_POOL_DESCRIPTION_RE = re.compile(rf"{re.escape(WIF_POOL_DESCRIPTION_PREFIX)}[0-9a-f]{{24}}\)")
_WIF_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_WIF_TRANSIENT_API_ERRORS: tuple[type[Exception], ...] = (
    gax.ResourceExhausted,
    gax.ServiceUnavailable,
    gax.InternalServerError,
    gax.GatewayTimeout,
    gax.DeadlineExceeded,
    gax.TooManyRequests,
    gax.RetryError,
)
_WIF_TRANSIENT_TIMEOUT_NAMES = frozenset({"Timeout", "ConnectTimeout", "ReadTimeout"})


def wif_pool_description(ownership_marker: str) -> str:
    """Build the exact native ownership description for a temporary SEC02 pool."""
    if re.fullmatch(r"[0-9a-f]{24}", ownership_marker) is None:
        raise ValueError("WIF ownership marker must contain exactly 24 lowercase hex characters")
    return f"{WIF_POOL_DESCRIPTION_PREFIX}{ownership_marker})"


def has_wif_pool_ownership(description: Any) -> bool:
    """Return whether a pool carries the exact SEC02 invocation marker shape."""
    return _WIF_POOL_DESCRIPTION_RE.fullmatch(str(description or "")) is not None


def _once(callback: Callable[[], None]) -> Callable[[], None]:
    """Return an idempotent ownership-handoff callback."""
    called = False

    def _invoke() -> None:
        nonlocal called
        if called:
            return
        callback()
        called = True

    return _invoke


def _create_may_have_committed(error: Exception, response: HttpResponse | None) -> bool:
    """Return whether a REST create failure may follow provider acceptance."""
    status = response.status_code if response is not None else None
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if status is None:
        return True
    code = int(status)
    return not (400 <= code < 500 and code not in {408, 429})


class HttpResponse(Protocol):
    """Small requests-compatible response surface used by the REST adapter."""

    status_code: int

    def json(self) -> dict[str, Any]: ...

    def raise_for_status(self) -> None: ...


class AuthorizedHttp(Protocol):
    """Authorized HTTP surface needed by the IAM workload-identity API."""

    def post(self, url: str, **kwargs: Any) -> HttpResponse: ...

    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...

    def delete(self, url: str, **kwargs: Any) -> HttpResponse: ...


def mint_with_propagation_retry[T](
    mint: Callable[[], T],
    *,
    timeout_seconds: float = IAM_PROPAGATION_TIMEOUT_SECONDS,
    poll_seconds: float = IAM_PROPAGATION_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry fresh-IAM 403/404 responses for one shared propagation deadline."""
    deadline = monotonic() + timeout_seconds
    attempt = 0
    last_error: Exception | None = None
    while True:
        attempt += 1
        try:
            return mint()
        except (gax.PermissionDenied, gax.Forbidden, gax.NotFound) as exc:
            last_error = exc

        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"IAM token-creator binding did not converge within {timeout_seconds:.0f}s "
                f"after {attempt} attempts: {last_error}"
            ) from last_error

        delay = min(poll_seconds, remaining)
        print(
            f"  IAM token mint not yet effective (attempt {attempt}); sleeping {delay:.0f}s",
            file=sys.stderr,
            flush=True,
        )
        sleep(delay)


class WorkloadIdentityRestClient:
    """Create and clean a temporary OIDC workload-identity fixture."""

    def __init__(
        self,
        session: AuthorizedHttp,
        project_number: str,
        *,
        operation_timeout_seconds: float = WIF_OPERATION_TIMEOUT_SECONDS,
        poll_seconds: float = WIF_OPERATION_POLL_SECONDS,
        request_attempts: int = WIF_REQUEST_ATTEMPTS,
        request_backoff_seconds: float = WIF_REQUEST_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._project_number = project_number
        self._operation_timeout_seconds = operation_timeout_seconds
        self._poll_seconds = poll_seconds
        self._request_attempts = request_attempts
        self._request_backoff_seconds = request_backoff_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    @staticmethod
    def _is_transient_request_error(error: Exception) -> bool:
        if isinstance(error, _WIF_TRANSIENT_API_ERRORS) or is_transport_disconnect(error):
            return True
        if type(error).__name__ in _WIF_TRANSIENT_TIMEOUT_NAMES:
            return True
        status = getattr(getattr(error, "response", None), "status_code", None)
        if not isinstance(status, int | str):
            return False
        try:
            return int(status) in _WIF_TRANSIENT_HTTP_STATUSES
        except ValueError:
            return False

    def _idempotent_request(
        self,
        method: Literal["get", "delete"],
        url: str,
        **kwargs: Any,
    ) -> HttpResponse:
        """Retry only bounded transient failures for an idempotent REST call."""
        request = getattr(self._session, method)
        for attempt in range(1, self._request_attempts + 1):
            try:
                response = request(url, **kwargs)
            except Exception as error:
                if not self._is_transient_request_error(error) or attempt >= self._request_attempts:
                    raise
            else:
                if response.status_code not in _WIF_TRANSIENT_HTTP_STATUSES:
                    return response
                if attempt >= self._request_attempts:
                    response.raise_for_status()
            self._sleep(self._request_backoff_seconds * attempt)
        raise AssertionError("bounded Workload Identity retry loop exhausted without a terminal result")

    @property
    def parent(self) -> str:
        return f"projects/{self._project_number}/locations/global"

    def pool_name(self, pool_id: str) -> str:
        return f"{self.parent}/workloadIdentityPools/{pool_id}"

    def provider_name(self, pool_id: str, provider_id: str) -> str:
        return f"{self.pool_name(pool_id)}/providers/{provider_id}"

    def provider_audience(self, pool_id: str, provider_id: str) -> str:
        return f"//iam.googleapis.com/{self.provider_name(pool_id, provider_id)}"

    def create_pool(
        self,
        pool_id: str,
        *,
        ownership_marker: str,
        on_accepted: Callable[[], None],
    ) -> None:
        description = wif_pool_description(ownership_marker)
        accept = _once(on_accepted)
        response: HttpResponse | None = None
        try:
            response = self._session.post(
                f"{IAM_API_BASE}/{self.parent}/workloadIdentityPools",
                params={"workloadIdentityPoolId": pool_id},
                json={
                    "displayName": "ISV SEC02 workload probe",
                    "description": description,
                },
                timeout=30,
            )
            self._wait_response_operation(
                response,
                f"create workload identity pool {pool_id}",
                on_accepted=accept,
            )
        except Exception as create_error:
            if not _create_may_have_committed(create_error, response):
                raise
            try:
                owned = self._resource_has_description(self.pool_name(pool_id), description)
            except Exception as readback_error:
                raise RuntimeError(
                    f"create workload identity pool {pool_id} failed ({create_error}); "
                    f"ownership readback failed ({readback_error})"
                ) from create_error
            if owned:
                accept()
                return
            raise

    def list_pools(self) -> list[dict[str, Any]]:
        """Return every active workload-identity pool across all response pages."""
        pools: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            response = self._idempotent_request(
                "get",
                f"{IAM_API_BASE}/{self.parent}/workloadIdentityPools",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            pools.extend(payload.get("workloadIdentityPools") or [])
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return pools

    def list_providers(self, pool_id: str) -> list[dict[str, Any]]:
        """Return every active provider under one workload-identity pool."""
        providers: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            response = self._idempotent_request(
                "get",
                f"{IAM_API_BASE}/{self.pool_name(pool_id)}/providers",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            providers.extend(payload.get("workloadIdentityPoolProviders") or [])
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return providers

    def create_oidc_provider(
        self,
        pool_id: str,
        provider_id: str,
        *,
        issuer_url: str,
        allowed_audience: str,
        ownership_marker: str,
        on_accepted: Callable[[], None],
    ) -> None:
        description = wif_pool_description(ownership_marker)
        accept = _once(on_accepted)
        response: HttpResponse | None = None
        try:
            response = self._session.post(
                f"{IAM_API_BASE}/{self.pool_name(pool_id)}/providers",
                params={"workloadIdentityPoolProviderId": provider_id},
                json={
                    "displayName": "ISV SEC02 OIDC provider",
                    "description": description,
                    "attributeMapping": {"google.subject": "assertion.sub"},
                    "oidc": {
                        "issuerUri": issuer_url,
                        "allowedAudiences": [allowed_audience],
                    },
                },
                timeout=30,
            )
            self._wait_response_operation(
                response,
                f"create workload identity provider {provider_id}",
                on_accepted=accept,
            )
        except Exception as create_error:
            if not _create_may_have_committed(create_error, response):
                raise
            try:
                owned = self._resource_has_description(
                    self.provider_name(pool_id, provider_id),
                    description,
                )
            except Exception as readback_error:
                raise RuntimeError(
                    f"create workload identity provider {provider_id} failed ({create_error}); "
                    f"ownership readback failed ({readback_error})"
                ) from create_error
            if owned:
                accept()
                return
            raise

    def delete_provider(self, pool_id: str, provider_id: str) -> None:
        self._delete_resource(
            self.provider_name(pool_id, provider_id),
            f"delete workload identity provider {provider_id}",
        )

    def delete_pool(self, pool_id: str) -> None:
        self._delete_resource(
            self.pool_name(pool_id),
            f"delete workload identity pool {pool_id}",
        )

    def _delete_resource(self, name: str, description: str) -> None:
        response = self._idempotent_request("delete", f"{IAM_API_BASE}/{name}", timeout=30)
        if response.status_code == 404:
            return
        self._wait_response_operation(response, description)

    def _resource_has_description(self, name: str, expected: str) -> bool:
        response = self._idempotent_request("get", f"{IAM_API_BASE}/{name}", timeout=30)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return response.json().get("description") == expected

    def _wait_response_operation(
        self,
        response: HttpResponse,
        description: str,
        *,
        on_accepted: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        response.raise_for_status()
        if on_accepted is not None:
            on_accepted()
        operation = response.json()
        name = str(operation.get("name") or "")
        if not name:
            raise RuntimeError(f"{description} returned no operation name")
        return self._wait_operation(name, description)

    def _wait_operation(self, name: str, description: str) -> dict[str, Any]:
        deadline = self._monotonic() + self._operation_timeout_seconds
        while True:
            response = self._idempotent_request("get", f"{IAM_API_BASE}/{name}", timeout=30)
            response.raise_for_status()
            operation = response.json()
            if operation.get("done"):
                error = operation.get("error")
                if error:
                    raise RuntimeError(f"{description} failed: {error}")
                return operation
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise RuntimeError(f"{description} did not complete within {self._operation_timeout_seconds:.0f}s")
            self._sleep(min(self._poll_seconds, remaining))


def sts_response_expiry(
    response: dict[str, Any],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> datetime:
    """Return the finite expiry represented by an RFC 8693 STS response."""
    if not response.get("access_token"):
        raise RuntimeError("STS token exchange returned no access_token")
    try:
        expires_in = int(response["expires_in"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("STS token exchange returned no valid expires_in") from exc
    if expires_in <= 0:
        raise RuntimeError(f"STS token exchange returned non-positive expires_in={expires_in}")
    return now() + timedelta(seconds=expires_in)
