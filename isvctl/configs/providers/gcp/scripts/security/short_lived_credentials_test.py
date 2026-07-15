#!/usr/bin/env python3
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

"""Verify bounded node- and workload-equivalent credentials without a VM.

The AWS reference validates two equivalent short-lived issuance paths instead
of running inside EC2/IRSA. GCP follows the same execution model:

* Node-equivalent: create a temporary service account, grant the configured
  runner service account Token Creator on that account, and mint an access
  token through a delegated IAM Credentials chain. The mint retries fresh-IAM
  403/404 responses for one 180-second propagation deadline.
* Workload-equivalent: create a temporary OIDC Workload Identity Federation
  pool/provider and exchange the configured OIDC fixture token through Google
  Security Token Service.

Both temporary fixtures are cleaned through guaranteed cleanup paths unless
the operator explicitly requests preservation for later teardown. Only expiry
and TTL metadata are emitted; credential material is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any, cast

import google.auth
import google.auth.transport.requests
from google.auth.transport.requests import AuthorizedSession
from google.cloud import iam_admin_v1, iam_credentials_v1, resourcemanager_v3
from google.iam.v1 import iam_policy_pb2, policy_pb2
from google.oauth2 import sts
from google.protobuf import duration_pb2

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parents[0]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix  # noqa: E402
from common.errors import handle_gcp_errors  # noqa: E402
from common.ownership import CREATED_BY_DESCRIPTION  # noqa: E402
from common.service_account import (  # noqa: E402
    create_service_account_resource,
    delete_service_account,
)
from short_lived_credentials_support import (  # noqa: E402
    ACCESS_TOKEN_TYPE,
    ID_TOKEN_TYPE,
    STS_TOKEN_ENDPOINT,
    TOKEN_EXCHANGE_GRANT_TYPE,
    AuthorizedHttp,
    WorkloadIdentityRestClient,
    mint_with_propagation_retry,
    sts_response_expiry,
)

DEFAULT_MAX_TTL_SECONDS = 43200
NODE_METHOD = "iam_credentials_delegated_generate_access_token"
WORKLOAD_METHOD = "workload_identity_federation_sts_exchange"
_WORKLOAD_LIFETIME_CAP_SECONDS = 3600
_WORKLOAD_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"
_WIF_PROVIDER_ID = "oidc"


class CredentialSurfaceUnavailable(RuntimeError):
    """The inputs needed to exercise an issuance surface are absent."""


def _ttl_seconds(expiry: datetime) -> int:
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return int((expiry - datetime.now(UTC)).total_seconds())


def _record_credential(
    result: dict[str, Any],
    *,
    expiry_key: str,
    ttl_key: str,
    ttl_field: str,
    expiry: datetime | None,
    max_ttl_seconds: int,
) -> None:
    if expiry is None:
        result["tests"][expiry_key]["error"] = "credential carried no expiry"
        result["tests"][ttl_key]["error"] = "no expiry returned -- TTL bound cannot be evaluated"
        return
    result["tests"][expiry_key]["passed"] = True
    ttl = _ttl_seconds(expiry)
    result[ttl_field] = ttl
    if 0 < ttl <= max_ttl_seconds:
        result["tests"][ttl_key]["passed"] = True
    else:
        result["tests"][ttl_key]["error"] = f"TTL {ttl}s outside (0, {max_ttl_seconds}s] bound"


def _project_number(project: str) -> str:
    resource = resourcemanager_v3.ProjectsClient().get_project(name=f"projects/{project}")
    number = str(resource.name).rsplit("/", 1)[-1]
    if not number.isdigit():
        raise RuntimeError(f"project lookup returned an invalid project number: {resource.name!r}")
    return number


def _bind_token_creator(node_email: str, runner_email: str) -> None:
    policy = policy_pb2.Policy(
        bindings=[
            policy_pb2.Binding(
                role=_TOKEN_CREATOR_ROLE,
                members=[f"serviceAccount:{runner_email}"],
            )
        ]
    )
    iam_admin_v1.IAMClient().set_iam_policy(
        request=iam_policy_pb2.SetIamPolicyRequest(
            resource=f"projects/-/serviceAccounts/{node_email}",
            policy=policy,
        )
    )


def _node_credential_expiry(
    project: str,
    runner_email: str,
    max_ttl_seconds: int,
    *,
    cleanup_enabled: bool,
) -> datetime | None:
    """Mint a node-equivalent credential through a temporary delegated SA."""
    runner_email = runner_email.strip()
    if not runner_email:
        raise CredentialSurfaceUnavailable(
            "node-equivalent issuance requires --impersonate-sa (set GCP_SECURITY_IMPERSONATION_SA)"
        )

    account_id = unique_suffix(f"isv-sec02-node-{token_hex(2)}")
    node_email = f"{account_id}@{project}.iam.gserviceaccount.com"
    node_created = False
    error: Exception | None = None
    expiry: datetime | None = None

    def _record_node_acceptance() -> None:
        nonlocal node_created
        node_created = True

    try:
        created = create_service_account_resource(
            project,
            account_id,
            display_name="ISV SEC02 node credential probe",
            description=f"SEC02 node-equivalent credential fixture ({CREATED_BY_DESCRIPTION}).",
            on_accepted=_record_node_acceptance,
        )
        node_email = created.email
        _bind_token_creator(node_email, runner_email)
        client = iam_credentials_v1.IAMCredentialsClient()
        lifetime = duration_pb2.Duration(seconds=min(max_ttl_seconds, _WORKLOAD_LIFETIME_CAP_SECONDS))

        def _mint() -> Any:
            return client.generate_access_token(
                name=f"projects/-/serviceAccounts/{node_email}",
                delegates=[f"projects/-/serviceAccounts/{runner_email}"],
                scope=list(_WORKLOAD_SCOPES),
                lifetime=lifetime,
            )

        response = mint_with_propagation_retry(_mint)
        expiry = cast(datetime | None, response.expire_time)
    except Exception as exc:
        error = exc

    if cleanup_enabled:
        cleanup_ok = not node_created or delete_service_account(node_email, project=project)
        if node_created and not cleanup_ok:
            cleanup_error = RuntimeError(f"cleanup failed for temporary service account {node_email}")
            if error:
                raise RuntimeError(f"{error}; {cleanup_error}") from error
            raise cleanup_error
    if error:
        raise error
    return expiry


def _workload_credential_expiry(
    project: str,
    *,
    issuer_url: str,
    audience: str,
    subject_token: str,
    cleanup_enabled: bool,
) -> datetime:
    """Exchange an OIDC fixture through a temporary WIF provider."""
    missing = [
        name
        for name, value in (
            ("--issuer-url", issuer_url),
            ("--audience", audience),
            ("--subject-token/OIDC_VALID_TOKEN", subject_token),
        )
        if not value.strip()
    ]
    if missing:
        raise CredentialSurfaceUnavailable("workload-equivalent issuance requires " + ", ".join(missing))

    pool_id = unique_suffix(f"isv-sec02-wif-{token_hex(2)}")
    ownership_marker = token_hex(12)
    credentials, _ = google.auth.default(scopes=list(_WORKLOAD_SCOPES))
    fixture = WorkloadIdentityRestClient(
        cast(AuthorizedHttp, AuthorizedSession(credentials)),
        _project_number(project),
    )
    error: Exception | None = None
    expiry: datetime | None = None
    pool_created = False
    provider_created = False

    def _record_pool_acceptance() -> None:
        nonlocal pool_created
        pool_created = True

    def _record_provider_acceptance() -> None:
        nonlocal provider_created
        provider_created = True

    try:
        fixture.create_pool(
            pool_id,
            ownership_marker=ownership_marker,
            on_accepted=_record_pool_acceptance,
        )
        fixture.create_oidc_provider(
            pool_id,
            _WIF_PROVIDER_ID,
            issuer_url=issuer_url.strip(),
            allowed_audience=audience.strip(),
            ownership_marker=ownership_marker,
            on_accepted=_record_provider_acceptance,
        )
        response = sts.Client(STS_TOKEN_ENDPOINT).exchange_token(
            google.auth.transport.requests.Request(),
            grant_type=TOKEN_EXCHANGE_GRANT_TYPE,
            subject_token=subject_token.strip(),
            subject_token_type=ID_TOKEN_TYPE,
            audience=fixture.provider_audience(pool_id, _WIF_PROVIDER_ID),
            scopes=list(_WORKLOAD_SCOPES),
            requested_token_type=ACCESS_TOKEN_TYPE,
        )
        expiry = sts_response_expiry(response)
    except Exception as exc:
        error = exc

    cleanup_errors: list[str] = []
    if cleanup_enabled and pool_created:
        pool_safe_to_delete = False
        if provider_created:
            try:
                fixture.delete_provider(pool_id, _WIF_PROVIDER_ID)
            except Exception as exc:
                cleanup_errors.append(f"provider cleanup failed: {exc}")
            else:
                pool_safe_to_delete = True
        else:
            try:
                remaining_providers = fixture.list_providers(pool_id)
            except Exception as exc:
                cleanup_errors.append(f"provider inventory failed; preserving pool {pool_id}: {exc}")
            else:
                if remaining_providers:
                    cleanup_errors.append(f"provider ownership was not established; preserving pool {pool_id}")
                else:
                    pool_safe_to_delete = True
        if pool_safe_to_delete:
            try:
                fixture.delete_pool(pool_id)
            except Exception as exc:
                cleanup_errors.append(f"pool cleanup failed: {exc}")

    if cleanup_errors:
        cleanup_error = "; ".join(cleanup_errors)
        if error:
            raise RuntimeError(f"{error}; {cleanup_error}") from error
        raise RuntimeError(cleanup_error)
    if error:
        raise error
    if expiry is None:
        raise RuntimeError("workload token exchange produced no expiry")
    return expiry


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Short-lived credentials test (SEC02-01)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--max-ttl-seconds", default=str(DEFAULT_MAX_TTL_SECONDS))
    parser.add_argument(
        "--impersonate-sa",
        default=os.environ.get("GCP_SECURITY_IMPERSONATION_SA", ""),
        help="Runner service account used as the delegated node-equivalent token minter",
    )
    parser.add_argument("--issuer-url", default=os.environ.get("OIDC_ISSUER_URL", ""))
    parser.add_argument("--audience", default=os.environ.get("OIDC_AUDIENCE", ""))
    parser.add_argument(
        "--subject-token",
        default=os.environ.get("OIDC_VALID_TOKEN", ""),
        help="OIDC subject token fixture (sensitive; defaults to OIDC_VALID_TOKEN)",
    )
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve run-owned fixtures for later teardown")
    args = parser.parse_args()

    try:
        max_ttl_seconds = int(args.max_ttl_seconds)
        if max_ttl_seconds < 1:
            raise ValueError("must be positive")
    except (TypeError, ValueError):
        print(
            json.dumps(
                {
                    "success": False,
                    "platform": "security",
                    "test_name": "short_lived_credentials_test",
                    "error": "--max-ttl-seconds must be a positive integer",
                    "tests": {},
                    "max_ttl_seconds": DEFAULT_MAX_TTL_SECONDS,
                },
                indent=2,
            )
        )
        return 1

    project = resolve_project(args.project)
    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "short_lived_credentials_test",
        "node_credential_method": NODE_METHOD,
        "workload_credential_method": WORKLOAD_METHOD,
        "node_credential_ttl_seconds": 0,
        "workload_credential_ttl_seconds": 0,
        "max_ttl_seconds": max_ttl_seconds,
        "cleanup_skipped": args.skip_destroy,
        "tests": {
            "node_credential_has_expiry": {"passed": False},
            "node_credential_ttl_within_bound": {"passed": False},
            "workload_credential_has_expiry": {"passed": False},
            "workload_credential_ttl_within_bound": {"passed": False},
        },
    }
    unavailable: dict[str, str] = {}

    try:
        node_expiry = _node_credential_expiry(
            project,
            args.impersonate_sa,
            max_ttl_seconds,
            cleanup_enabled=not args.skip_destroy,
        )
        _record_credential(
            result,
            expiry_key="node_credential_has_expiry",
            ttl_key="node_credential_ttl_within_bound",
            ttl_field="node_credential_ttl_seconds",
            expiry=node_expiry,
            max_ttl_seconds=max_ttl_seconds,
        )
    except CredentialSurfaceUnavailable as exc:
        unavailable["node"] = str(exc)
        result["tests"]["node_credential_has_expiry"]["error"] = str(exc)
        result["tests"]["node_credential_ttl_within_bound"]["error"] = str(exc)
    except Exception as exc:
        result["tests"]["node_credential_has_expiry"]["error"] = str(exc)
        result["tests"]["node_credential_ttl_within_bound"]["error"] = str(exc)

    try:
        workload_expiry = _workload_credential_expiry(
            project,
            issuer_url=args.issuer_url,
            audience=args.audience,
            subject_token=args.subject_token,
            cleanup_enabled=not args.skip_destroy,
        )
        _record_credential(
            result,
            expiry_key="workload_credential_has_expiry",
            ttl_key="workload_credential_ttl_within_bound",
            ttl_field="workload_credential_ttl_seconds",
            expiry=workload_expiry,
            max_ttl_seconds=max_ttl_seconds,
        )
    except CredentialSurfaceUnavailable as exc:
        unavailable["workload"] = str(exc)
        result["tests"]["workload_credential_has_expiry"]["error"] = str(exc)
        result["tests"]["workload_credential_ttl_within_bound"]["error"] = str(exc)
    except Exception as exc:
        result["tests"]["workload_credential_has_expiry"]["error"] = str(exc)
        result["tests"]["workload_credential_ttl_within_bound"]["error"] = str(exc)

    if set(unavailable) == {"node", "workload"}:
        print(
            json.dumps(
                {
                    "success": True,
                    "platform": "security",
                    "test_name": "short_lived_credentials_test",
                    "skipped": True,
                    "skip_reason": (
                        "credential issuance surfaces are unavailable: "
                        f"node ({unavailable['node']}); workload ({unavailable['workload']})"
                    ),
                    "tests": {},
                    "max_ttl_seconds": max_ttl_seconds,
                },
                indent=2,
            )
        )
        return 0

    result["success"] = all(probe["passed"] for probe in result["tests"].values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
