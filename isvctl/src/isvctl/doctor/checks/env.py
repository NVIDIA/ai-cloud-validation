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

"""Environment-variable presence checks.

Values are never read into CheckResult.message/detail — only set/unset state
is reported, so this category is safe to print and to emit as JSON.
"""

import base64
import json
import os
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from isvctl.config.env_catalog import ENV_VARS, Requirement
from isvctl.doctor.result import CategoryReport, CheckResult, Status
from isvctl.redaction import redact_text

_NICO_TOKEN_TIMEOUT_SECONDS = 30

# The variable catalog lives in `isvctl.config.env_catalog` so `doctor` and
# `isvctl configure` share one source of truth.


def _status_for(requirement: Requirement, present: bool) -> Status:
    """Map (requirement, presence) to a Status."""
    if present:
        return Status.OK
    match requirement:
        case Requirement.REQUIRED:
            return Status.FAIL
        case Requirement.RECOMMENDED:
            return Status.WARN
        case Requirement.OPTIONAL:
            return Status.SKIP


def _aws_credentials_present() -> bool:
    """Mirror boto3's credential chain closely enough to avoid false failures.

    boto3 accepts static keys, a named profile, web-identity/assume-role, or a
    shared credentials/config file (which also backs SSO sessions). We only
    check that *a* source is configured — never that it is valid.
    """
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True
    if os.environ.get("AWS_PROFILE"):
        return True
    if os.environ.get("AWS_ROLE_ARN") and os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
        return True
    aws_dir = Path.home() / ".aws"
    return (aws_dir / "credentials").is_file() or (aws_dir / "config").is_file()


def _aws_region_present() -> bool:
    """AWS scripts need a region; it can come from env or a shared config file."""
    if os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
        return True
    return (Path.home() / ".aws" / "config").is_file()


def _check_aws_provider() -> list[CheckResult]:
    """AWS readiness, modeled on boto3's credential resolution.

    Replaces a naive "static keys must be set" rule that would falsely FAIL for
    users authenticated via AWS_PROFILE, SSO, or an instance/role credential.
    """
    creds_ok = _aws_credentials_present()
    region_ok = _aws_region_present()
    return [
        CheckResult(
            name="AWS credentials",
            status=Status.OK if creds_ok else Status.FAIL,
            message="resolved" if creds_ok else "no credential source found",
            remediation=None
            if creds_ok
            else "export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, set AWS_PROFILE, "
            "or run `aws configure` / `aws sso login`",
            group="AWS",
        ),
        CheckResult(
            name="AWS region",
            status=Status.OK if region_ok else Status.FAIL,
            message="resolved" if region_ok else "no region configured",
            remediation=None
            if region_ok
            else "export AWS_REGION (or AWS_DEFAULT_REGION), or set `region` in ~/.aws/config",
            group="AWS",
        ),
    ]


def _env_value(name: str) -> str:
    """Return a stripped environment value or an empty string."""
    return os.environ.get(name, "").strip()


def _request_nico_oidc_token(
    *,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    scope: str,
) -> str:
    """Request a NICo access token with OIDC client_credentials."""
    token_endpoint = _discover_nico_oidc_token_endpoint(issuer_url)
    form = {"grant_type": "client_credentials"}
    if scope:
        form["scope"] = scope
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = Request(
        token_endpoint,
        data=urlencode(form).encode(),
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_NICO_TOKEN_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        detail = f"HTTP {exc.code}"
        if exc.fp:
            body = redact_text(exc.fp.read().decode(errors="replace")[:300])
            if body:
                detail = f"{detail}: {body}"
        raise RuntimeError(f"OIDC token request failed with {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"OIDC token request failed: {exc.reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("OIDC token response was not valid JSON") from exc

    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("OIDC token response did not contain access_token") from None
    return token.strip()


def _discover_nico_oidc_token_endpoint(issuer_url: str) -> str:
    """Resolve the OIDC token endpoint from issuer metadata."""
    request = Request(f"{issuer_url.rstrip('/')}/.well-known/openid-configuration")
    try:
        with urlopen(request, timeout=_NICO_TOKEN_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        detail = f"HTTP {exc.code}"
        if exc.fp:
            body = redact_text(exc.fp.read().decode(errors="replace")[:300])
            if body:
                detail = f"{detail}: {body}"
        raise RuntimeError(f"OIDC discovery failed with {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"OIDC discovery failed: {exc.reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("OIDC discovery response was not valid JSON") from exc

    token_endpoint = payload.get("token_endpoint")
    if not isinstance(token_endpoint, str) or not token_endpoint.strip():
        raise RuntimeError("OIDC discovery response did not contain token_endpoint") from None
    return token_endpoint.strip()


def _resolve_nico_doctor_token() -> tuple[str, str]:
    """Resolve NICo auth for doctor and return token plus safe display label."""
    bearer = _env_value("NICO_BEARER_TOKEN")
    if bearer:
        return bearer, "bearer token configured"

    issuer_url = _env_value("NICO_SSA_ISSUER")
    client_id = _env_value("NICO_CLIENT_ID")
    client_secret = _env_value("NICO_CLIENT_SECRET")
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
        raise RuntimeError(
            f"set NICO_BEARER_TOKEN or configure OIDC client_credentials (missing: {', '.join(missing)})"
        )

    token = _request_nico_oidc_token(
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=_env_value("NICO_OIDC_SCOPE"),
    )
    return token, "OIDC client_credentials configured"


def _probe_nico_api(organization: str, site_id: str, api_base: str, token: str) -> bool:
    """Probe the NICo site endpoint with the resolved token."""
    url = f"{api_base.rstrip('/')}/{organization}/carbide/site/{site_id}"
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=10) as response:
        response.read()
    return True


def _check_nico_provider() -> list[CheckResult]:
    """NICo readiness checks for `isvctl doctor --provider nico`."""
    api_base = _env_value("NICO_API_BASE")
    organization = _env_value("NICO_ORGANIZATION")
    site_id = _env_value("NICO_SITE_ID")
    results = [
        CheckResult(
            name="NICO_API_BASE",
            status=Status.OK if api_base else Status.FAIL,
            message="set" if api_base else "unset (required)",
            remediation=None if api_base else "export NICO_API_BASE with the NICo API base URL",
            group="NICo",
        ),
        CheckResult(
            name="NICO_ORGANIZATION",
            status=Status.OK if organization else Status.FAIL,
            message="set" if organization else "unset (required)",
            remediation=None
            if organization
            else "export NICO_ORGANIZATION with the NICo organization name used in the API path",
            group="NICo",
        ),
        CheckResult(
            name="NICO_SITE_ID",
            status=Status.OK if site_id else Status.FAIL,
            message="set" if site_id else "unset (required)",
            remediation=None if site_id else "export NICO_SITE_ID with the Forge site UUID",
            group="NICo",
        ),
    ]

    token = ""
    try:
        token, auth_message = _resolve_nico_doctor_token()
    except RuntimeError as exc:
        is_missing_config = str(exc).startswith("set NICO_BEARER_TOKEN")
        results.append(
            CheckResult(
                name="NICo auth",
                status=Status.FAIL,
                message="not configured" if is_missing_config else "auth failed",
                remediation=str(exc),
                group="NICo",
            )
        )
    else:
        results.append(
            CheckResult(
                name="NICo auth",
                status=Status.OK,
                message=auth_message,
                group="NICo",
            )
        )

    if not api_base or not organization or not site_id or not token:
        results.append(
            CheckResult(
                name="NICo API",
                status=Status.SKIP,
                message="skipped until NICo config and auth are complete",
                group="NICo",
            )
        )
        return results

    try:
        reachable = _probe_nico_api(organization, site_id, api_base, token)
    except (HTTPError, URLError, OSError, RuntimeError) as exc:
        results.append(
            CheckResult(
                name="NICo API",
                status=Status.FAIL,
                message="unreachable or unauthorized",
                detail=str(exc),
                remediation="check NICO_API_BASE, NICO_ORGANIZATION, NICO_SITE_ID, credentials, and any port-forward",
                group="NICo",
            )
        )
    else:
        results.append(
            CheckResult(
                name="NICo API",
                status=Status.OK if reachable else Status.FAIL,
                message="reachable" if reachable else "unreachable",
                group="NICo",
            )
        )

    return results


# Provider-conditional readiness checks. A selected provider gets a real
# capability check (credential chain, etc.) appended to the env category.
_PROVIDER_CHECKS: dict[str, Callable[[], list[CheckResult]]] = {
    "aws": _check_aws_provider,
    "nico": _check_nico_provider,
}
_PROVIDER_STRICT_ENV_VARS: dict[str, frozenset[str]] = {
    "nico": frozenset({"NICO_API_BASE", "NICO_ORGANIZATION", "NICO_SITE_ID"}),
}


def check_env(providers: list[str] | None = None) -> CategoryReport:
    """Run the env category.

    Args:
        providers: Provider names whose readiness checks (e.g. the AWS
            credential chain) get appended.

    Returns:
        CategoryReport. Values are never recorded — only set/unset state.
    """
    results: list[CheckResult] = []
    provider_strict_vars = {
        name for prov in providers or [] for name in _PROVIDER_STRICT_ENV_VARS.get(prov, frozenset())
    }
    for var in ENV_VARS:
        if var.name in provider_strict_vars:
            continue
        # "set" means exported, even if empty — distinguishing set-but-empty
        # from truly unset (a bare `bool()` would mis-report `FOO=` as unset).
        present = os.environ.get(var.name) is not None
        status = _status_for(var.requirement, present)

        if present:
            message = "set"
        elif status == Status.FAIL:
            message = "unset (required)"
        elif status == Status.WARN:
            message = "unset (recommended)"
        else:
            message = "unset (optional)"

        results.append(
            CheckResult(
                name=var.name,
                status=status,
                message=message,
                remediation=None if present else var.hint,
                group=var.group,
            )
        )

    for prov in providers or []:
        provider_check = _PROVIDER_CHECKS.get(prov)
        if provider_check is not None:
            results.extend(provider_check())

    return CategoryReport(name="env", results=results)
