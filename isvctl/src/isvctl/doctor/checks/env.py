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

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from isvctl.doctor.result import CategoryReport, CheckResult, Status


class Requirement(StrEnum):
    """How strictly an env var is needed."""

    REQUIRED = "required"  # missing → FAIL
    RECOMMENDED = "recommended"  # missing → WARN
    OPTIONAL = "optional"  # missing → SKIP (informational only)


@dataclass(frozen=True)
class _Var:
    """One environment variable to check."""

    name: str
    group: str
    requirement: Requirement
    hint: str


# Variable table — single source of truth for which env vars `doctor` knows
# about and how it classifies them. Keep grouped for stable rendering order.
_VARS: tuple[_Var, ...] = (
    # ISV Lab Service
    _Var(
        "ISV_SERVICE_ENDPOINT",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed to upload results to ISV Lab Service",
    ),
    _Var(
        "ISV_SSA_ISSUER",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed for SSA auth against ISV Lab Service",
    ),
    _Var(
        "ISV_CLIENT_ID",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed to authenticate result uploads",
    ),
    _Var(
        "ISV_CLIENT_SECRET",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed to authenticate result uploads",
    ),
    # NGC
    _Var(
        "NGC_API_KEY",
        "NGC",
        Requirement.RECOMMENDED,
        "needed for NIM workloads and the NGC container registry",
    ),
    _Var(
        "NGC_NIM_API_KEY",
        "NGC",
        Requirement.OPTIONAL,
        "alternative to NGC_API_KEY for NIM workloads",
    ),
    # AWS — informational only. Static keys are just one of several credential
    # sources boto3 accepts; `--provider aws` runs `_check_aws_provider` which
    # validates the whole chain instead of demanding these specific vars.
    _Var(
        "AWS_ACCESS_KEY_ID",
        "AWS",
        Requirement.OPTIONAL,
        "one way to supply AWS credentials (see also AWS_PROFILE / SSO)",
    ),
    _Var(
        "AWS_SECRET_ACCESS_KEY",
        "AWS",
        Requirement.OPTIONAL,
        "one way to supply AWS credentials (see also AWS_PROFILE / SSO)",
    ),
    _Var(
        "AWS_REGION",
        "AWS",
        Requirement.OPTIONAL,
        "AWS region; may also come from AWS_DEFAULT_REGION or ~/.aws/config",
    ),
    # Flags — informational only.
    _Var(
        "KUBECTL",
        "Flags",
        Requirement.OPTIONAL,
        "override the kubectl command (POSIX shlex split)",
    ),
    _Var(
        "ISVCTL_DEMO_MODE",
        "Flags",
        Requirement.OPTIONAL,
        "set to '1' to use my-isv demo stubs",
    ),
    _Var(
        "ISVTEST_INCLUDE_UNRELEASED",
        "Flags",
        Requirement.OPTIONAL,
        "include unreleased validations",
    ),
    _Var(
        "AWS_SKIP_TEARDOWN",
        "Flags",
        Requirement.OPTIONAL,
        "skip AWS teardown phase",
    ),
)


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


# Provider-conditional readiness checks. A selected provider gets a real
# capability check (credential chain, etc.) appended to the env category.
_PROVIDER_CHECKS: dict[str, Callable[[], list[CheckResult]]] = {
    "aws": _check_aws_provider,
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
    for var in _VARS:
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
