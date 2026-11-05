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

"""ISV Lab Service credential and lab-access check.

Verifies, without creating a test run, that the configured ``ISV_CLIENT_ID`` /
``ISV_CLIENT_SECRET`` can obtain a token and that the token can see a given lab
(``GET /v1/labs/{lab_id}``, allowed with the upload scopes). Secrets and tokens are never recorded in results.
"""

import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from isvreporter.config import get_endpoint, get_ssa_issuer

from isvctl.doctor.result import CategoryReport, CheckResult, Status

_TIMEOUT_SECONDS = 30
_GROUP = "ISV Lab Service"
_UPLOAD_SCOPES = "create-isv-lab-test-run update-isv-lab-test-run"


class _TokenError(Exception):
    """Token request failed; ``status`` is the HTTP code when there was a response."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _request_token(ssa_issuer: str, client_id: str, client_secret: str, scope: str) -> str:
    """Request a client_credentials token from SSA for ``scope``."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        request = Request(
            f"{ssa_issuer}/token",
            data=urlencode({"scope": scope, "grant_type": "client_credentials"}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
            method="POST",
        )
    except ValueError as exc:
        raise _TokenError("invalid ISV_SSA_ISSUER URL") from exc
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return str(json.loads(response.read().decode())["access_token"])
    except HTTPError as exc:
        raise _TokenError(f"HTTP {exc.code}", exc.code) from exc
    except OSError as exc:
        raise _TokenError(f"cannot reach SSA issuer: {getattr(exc, 'reason', exc)}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
        raise _TokenError("unexpected token response format") from exc


def _fail(name: str, message: str, remediation: str, detail: str | None = None) -> CheckResult:
    return CheckResult(
        name=name, status=Status.FAIL, message=message, detail=detail, remediation=remediation, group=_GROUP
    )


def _skip(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, status=Status.SKIP, message=message, group=_GROUP)


def check_lab(lab_id: int) -> CategoryReport:
    """Check ISV Lab Service credentials and access to ``lab_id``.

    Args:
        lab_id: Lab to look up with ``GET /v1/labs/{lab_id}``.

    Returns:
        CategoryReport named ``lab`` with credentials, token and lab-access rows.
    """
    endpoint = get_endpoint().rstrip("/")
    ssa_issuer = get_ssa_issuer().rstrip("/")
    client_id = os.environ.get("ISV_CLIENT_ID")
    client_secret = os.environ.get("ISV_CLIENT_SECRET")

    missing = [
        name
        for name, value in (
            ("ISV_SERVICE_ENDPOINT", endpoint),
            ("ISV_SSA_ISSUER", ssa_issuer),
            ("ISV_CLIENT_ID", client_id),
            ("ISV_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        return CategoryReport(
            name="lab",
            results=[
                _fail(
                    "ISV Lab Service config",
                    f"unset: {', '.join(missing)}",
                    "export the missing variables (see `isvctl config show`)",
                ),
                _skip("SSA token", "skipped until config is complete"),
                _skip(f"Lab {lab_id}", "skipped until config is complete"),
            ],
        )
    assert client_id and client_secret  # narrowed by the missing check above

    results: list[CheckResult] = []
    try:
        token = _request_token(ssa_issuer, client_id, client_secret, _UPLOAD_SCOPES)
    except _TokenError as exc:
        results.append(
            _fail(
                "SSA token",
                f"token request failed ({exc})",
                "check ISV_CLIENT_ID / ISV_CLIENT_SECRET and ISV_SSA_ISSUER",
            )
        )
        results.append(_skip(f"Lab {lab_id}", "skipped: no token"))
        return CategoryReport(name="lab", results=results)

    results.append(CheckResult(name="SSA token", status=Status.OK, message="credentials accepted", group=_GROUP))

    try:
        request = Request(f"{endpoint}/v1/labs/{lab_id}", headers={"Authorization": f"Bearer {token}"})
    except ValueError:
        results.append(
            _fail(f"Lab {lab_id}", "invalid ISV_SERVICE_ENDPOINT URL", "set ISV_SERVICE_ENDPOINT to a full URL")
        )
        return CategoryReport(name="lab", results=results)
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            response.read()
    except HTTPError as exc:
        hints = {
            401: ("token rejected by the service", "check ISV_SERVICE_ENDPOINT matches the SSA issuer's environment"),
            403: ("forbidden: no access to this lab's ISV", "use credentials issued for the ISV that owns this lab"),
            404: ("lab not found", "check --lab-id and that ISV_SERVICE_ENDPOINT points at the right environment"),
        }
        message, remediation = hints.get(exc.code, (f"unexpected HTTP {exc.code}", "retry; check service status"))
        results.append(_fail(f"Lab {lab_id}", message, remediation, detail=f"GET /v1/labs/{lab_id} -> {exc.code}"))
    except (URLError, OSError) as exc:
        results.append(
            _fail(
                f"Lab {lab_id}",
                "ISV Lab Service unreachable",
                "check ISV_SERVICE_ENDPOINT and network/VPN",
                detail=str(getattr(exc, "reason", exc)),
            )
        )
    else:
        results.append(
            CheckResult(name=f"Lab {lab_id}", status=Status.OK, message="exists and is accessible", group=_GROUP)
        )
    return CategoryReport(name="lab", results=results)
