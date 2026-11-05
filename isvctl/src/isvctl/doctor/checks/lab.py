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

import os
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from isvreporter.auth import TokenError, is_secure_url, request_jwt_token
from isvreporter.config import get_endpoint, get_ssa_issuer

from isvctl.doctor.result import CategoryReport, CheckResult, Status

_TIMEOUT_SECONDS = 10
_GROUP = "ISV Lab Service"
_CONFIG_ROW = "ISV Lab Service config"
_TOKEN_ROW = "SSA token"

# HTTP status from GET /v1/labs/{id} -> (message, remediation)
_LAB_HTTP_HINTS: dict[int, tuple[str, str]] = {
    401: ("token rejected by the service", "check ISV_SERVICE_ENDPOINT matches the SSA issuer's environment"),
    403: ("forbidden: no access to this lab's ISV", "use credentials issued for the ISV that owns this lab"),
    404: ("lab not found", "check --lab-id and that ISV_SERVICE_ENDPOINT points at the right environment"),
}


def _row(
    name: str, status: Status, message: str, remediation: str | None = None, detail: str | None = None
) -> CheckResult:
    """Build a result row in the ISV Lab Service group."""
    return CheckResult(name=name, status=status, message=message, detail=detail, remediation=remediation, group=_GROUP)


def _lab_results(lab_id: int) -> list[CheckResult]:
    """Return the config, token and lab-access rows, stopping at the first failing stage."""
    lab_row = f"Lab {lab_id}"
    endpoint = get_endpoint().rstrip("/")
    ssa_issuer = get_ssa_issuer().rstrip("/")
    client_id = os.environ.get("ISV_CLIENT_ID", "")
    client_secret = os.environ.get("ISV_CLIENT_SECRET", "")

    urls = {"ISV_SERVICE_ENDPOINT": endpoint, "ISV_SSA_ISSUER": ssa_issuer}
    values = {**urls, "ISV_CLIENT_ID": client_id, "ISV_CLIENT_SECRET": client_secret}
    unset = [name for name, value in values.items() if not value]
    not_urls = [name for name, value in urls.items() if value and not is_secure_url(value)]
    if unset or not_urls:
        problems = [
            f"{label}: {', '.join(names)}"
            for label, names in (("unset", unset), ("not an https URL", not_urls))
            if names
        ]
        return [
            _row(
                _CONFIG_ROW,
                Status.FAIL,
                "; ".join(problems),
                "set these variables; endpoints must be https URLs (see `isvctl config show`)",
            ),
            _row(_TOKEN_ROW, Status.SKIP, "skipped until config is complete"),
            _row(lab_row, Status.SKIP, "skipped until config is complete"),
        ]

    try:
        token = request_jwt_token(ssa_issuer, client_id, client_secret, timeout=_TIMEOUT_SECONDS)
    except TokenError as exc:
        return [
            _row(
                _TOKEN_ROW,
                Status.FAIL,
                f"token request failed ({exc})",
                "check ISV_CLIENT_ID / ISV_CLIENT_SECRET and ISV_SSA_ISSUER",
            ),
            _row(lab_row, Status.SKIP, "skipped: no token"),
        ]
    token_ok = _row(_TOKEN_ROW, Status.OK, "credentials accepted")

    request = Request(f"{endpoint}/v1/labs/{lab_id}")
    # Unredirected so a redirect never carries the bearer token to another host.
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS):
            pass
    except HTTPError as exc:
        message, remediation = _LAB_HTTP_HINTS.get(
            exc.code, (f"unexpected HTTP {exc.code}", "retry; check service status")
        )
        return [
            token_ok,
            _row(lab_row, Status.FAIL, message, remediation, detail=f"GET /v1/labs/{lab_id} -> {exc.code}"),
        ]
    except (OSError, HTTPException) as exc:
        return [
            token_ok,
            _row(
                lab_row,
                Status.FAIL,
                "ISV Lab Service unreachable",
                "check ISV_SERVICE_ENDPOINT and network/VPN",
                detail=str(getattr(exc, "reason", exc)),
            ),
        ]
    return [token_ok, _row(lab_row, Status.OK, "exists and is accessible")]


def check_lab(lab_id: int) -> CategoryReport:
    """Check ISV Lab Service credentials and access to ``lab_id``.

    Args:
        lab_id: Lab to look up with ``GET /v1/labs/{lab_id}``.

    Returns:
        CategoryReport named ``lab`` with config, token and lab-access rows.
    """
    return CategoryReport(name="lab", results=_lab_results(lab_id))
