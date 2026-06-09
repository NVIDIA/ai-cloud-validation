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

"""Minimal NVIDIA Unified Fabric Manager (UFM) REST client.

The InfiniBand subnet-manager security keys (M_Key, SM_Key, SA_Key, and the
``m_key_per_port`` protection flag) are configured on the UFM host and are not
surfaced by NICo's public REST API. They are, however, readable from UFM's own
REST API at ``/app/smconf`` -- the same endpoint NICo's IbFabricMonitor uses to
decide whether a fabric is securely configured.

This client mirrors NICo's UFM auth model (``infra-controller``
``crates/ib-fabric/src/ib/ufmclient``):

- Token auth (``UFM_TOKEN``): base path ``/ufmRestV3``, ``Authorization: Basic <token>``.
- Basic auth (``UFM_USERNAME`` / ``UFM_PASSWORD``): base path ``/ufmRest``,
  ``Authorization: Basic <base64(user:pass)>``.

UFM commonly serves a self-signed certificate, so TLS verification can be
disabled with ``UFM_ALLOW_INSECURE=1`` (verification is on by default).

Reference:
    infra-controller crates/ib-fabric/src/ib/ufmclient/{mod.rs,rest.rs}
    NVIDIA UFM Enterprise REST API Guide
"""

from __future__ import annotations

import base64
import json
import os
import ssl
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

UFM_TIMEOUT_SECONDS = 30


class UfmAuthError(RuntimeError):
    """Raised when UFM authentication cannot be resolved."""


class UfmAuth(NamedTuple):
    """Resolved UFM connection: base URL, auth header, TLS posture, source label."""

    base_url: str
    auth_header: str
    insecure: bool
    source: str


def _env(name: str) -> str:
    """Return a stripped environment value or an empty string."""
    return os.environ.get(name, "").strip()


def ufm_configured() -> bool:
    """Return whether enough UFM configuration is present to attempt a connection."""
    return bool(_env("UFM_ADDRESS") and (_env("UFM_TOKEN") or (_env("UFM_USERNAME") and _env("UFM_PASSWORD"))))


def _insecure_enabled() -> bool:
    """Return whether TLS verification to UFM should be disabled."""
    return _env("UFM_ALLOW_INSECURE").lower() in {"1", "true", "yes", "on"}


def resolve_ufm_auth() -> UfmAuth:
    """Resolve UFM REST authentication from environment variables.

    Resolution order:
    1. ``UFM_TOKEN`` (UFM access token) against the ``/ufmRestV3`` base path.
    2. ``UFM_USERNAME`` / ``UFM_PASSWORD`` against the ``/ufmRest`` base path.

    Raises:
        UfmAuthError: when ``UFM_ADDRESS`` or credentials are missing.
    """
    address = _env("UFM_ADDRESS")
    if not address:
        raise UfmAuthError("UFM access is not configured; set UFM_ADDRESS")

    parsed = urlparse(address if "://" in address else f"https://{address}")
    if not parsed.hostname:
        raise UfmAuthError(f"UFM_ADDRESS is not a valid URL: {address!r}")
    scheme = parsed.scheme or "https"
    netloc = parsed.hostname + (f":{parsed.port}" if parsed.port else "")

    token = _env("UFM_TOKEN")
    if token:
        base_path = "ufmRestV3"
        auth_header = f"Basic {token}"
        source = "UFM_TOKEN"
    else:
        username = _env("UFM_USERNAME")
        password = _env("UFM_PASSWORD")
        if not (username and password):
            raise UfmAuthError("UFM authentication is not configured; set UFM_TOKEN or UFM_USERNAME and UFM_PASSWORD")
        base_path = "ufmRest"
        auth_header = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        source = "UFM_USERNAME"

    return UfmAuth(
        base_url=f"{scheme}://{netloc}/{base_path}",
        auth_header=auth_header,
        insecure=_insecure_enabled(),
        source=source,
    )


def ufm_get(path: str, auth: UfmAuth, *, timeout: int = UFM_TIMEOUT_SECONDS) -> Any:
    """Make an authenticated GET request to a UFM REST path.

    Args:
        path: API path relative to the base path (e.g. "app/smconf").
        auth: Resolved UFM auth.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response.

    Raises:
        HTTPError / URLError: on transport/HTTP failures.
        UfmAuthError: when UFM signals a not-found via its ``{}``-with-200 quirk
            or returns a non-JSON body.
    """
    url = f"{auth.base_url}/{path.strip('/')}"
    req = Request(url, headers={"Authorization": auth.auth_header})
    context = ssl._create_unverified_context() if auth.insecure else None

    with urlopen(req, timeout=timeout, context=context) as resp:
        body = resp.read().decode()

    # UFM sometimes returns 200 with an empty object to mean "not found".
    if body.strip() == "{}":
        raise UfmAuthError(f"UFM returned an empty body for {path!r} (resource not found)")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise UfmAuthError(f"UFM response for {path!r} was not valid JSON") from e


def get_sm_config(auth: UfmAuth, *, timeout: int = UFM_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Return the OpenSM configuration UFM exposes at ``/app/smconf``.

    The response carries the subnet-manager security keys: ``m_key`` (Management
    Key), ``sm_key``, ``sa_key``, and the ``m_key_per_port`` protection flag.
    """
    config = ufm_get("app/smconf", auth, timeout=timeout)
    if not isinstance(config, dict):
        raise UfmAuthError("UFM /app/smconf did not return an object")
    return config


def parse_key_value(value: Any) -> int | None:
    """Parse a UFM key value (hex "0x10" or decimal) to an int, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        return int(text, 16) if text.startswith("0x") else int(text, 10)
    except ValueError:
        return None


def describe_http_error(e: HTTPError) -> str:
    """Return a concise, body-trimmed description of a UFM HTTP error."""
    body = ""
    if e.fp:
        body = e.fp.read().decode(errors="replace")[:200]
    detail = f"HTTP {e.code}"
    return f"{detail}: {body}" if body else detail


def describe_url_error(e: URLError) -> str:
    """Return a concise description of a UFM connection error."""
    return f"connection failed: {e.reason}"
