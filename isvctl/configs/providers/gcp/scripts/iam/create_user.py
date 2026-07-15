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

"""Create a GCP service account + short-lived impersonation token (IAM create_user).

Google Cloud has no human IAM users; the managed application principal is a
service account. This stub maps the suite's AWS-shaped create_user contract
onto a service account:

  1. Create a uniquely-named service account in the resolved project.
  2. Grant the ADC principal roles/iam.serviceAccountTokenCreator on the new
     service account's resource policy.
  3. Mint a short-lived (600s) OAuth2 access token for the service account via
     IAMCredentials.generateAccessToken, retrying while the tokenCreator
     binding propagates (eventually-consistent, observed up to ~180s on
     hardened orgs -> 12 x 15s budget).

The AWS-shaped output names are preserved for contract compatibility while
their GCP meaning is documented: ``access_key_id`` is the service account
unique_id (NON-SECRET; equals tokeninfo.azp) and ``secret_access_key`` is the
short-lived OAuth2 access token (sensitive, self-expiring — no key file).

If credential minting fails after the service account is created, the
partially-created service account is deleted before returning failure so a
partial failure does not leak a project-level service account.

Usage:
    python3 create_user.py --username isv-test-user --create-access-key --project=my-project

Output JSON:
{
    "success": true,
    "platform": "iam",
    "username": "isv-test-user-ab12-cd34ef56@my-project.iam.gserviceaccount.com",
    "user_id": "1234567890",
    "service_account_name": "projects/my-project/serviceAccounts/...",
    "access_key_id": "1234567890",
    "secret_access_key": "ya29...",
    "token_expiry": "2026-06-05T12:34:56+00:00",
    "project": "my-project"
}
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import (
    TRANSIENT_EXCEPTIONS,
    classify_gcp_error,
    handle_gcp_errors,
    modify_iam_policy_with_retry,
)
from common.service_account import delete_service_account, resolve_principal_member
from google.api_core import exceptions as gax
from google.cloud import iam_admin_v1, iam_credentials_v1
from google.iam.v1 import iam_policy_pb2, policy_pb2
from google.protobuf import duration_pb2

# tokenCreator binding -> token mint propagation budget. A freshly-granted
# serviceAccountTokenCreator binding is not effective for generateAccessToken
# immediately; hardened orgs have required up to ~180s. 12 x 15s = 180s.
TOKEN_MINT_MAX_RETRIES = 12
TOKEN_MINT_RETRY_DELAY_SECONDS = 15
TOKEN_LIFETIME_SECONDS = 600

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"

# GCP service-account ID local-part: 6-30 chars, must start with a lowercase
# letter, then lowercase letters / digits / hyphens.
_SA_ID_MAX_LEN = 30

# generateAccessToken propagation shapes worth retrying: the tokenCreator
# binding is not yet effective (PermissionDenied), the service account is not
# yet visible (NotFound), or a transient backend error.
_RETRYABLE_MINT: tuple[type[Exception], ...] = (
    gax.PermissionDenied,
    gax.NotFound,
    *TRANSIENT_EXCEPTIONS,
)


def _service_account_id(username: str) -> str:
    """Normalize ``username`` into a unique, valid service-account ID (<=30 chars).

    GCP service-account IDs are lowercase RFC1035-ish identifiers (6-30 chars,
    must start with a letter). The 30-char cap means a run-id suffix alone is
    not collision-safe under truncation (a wider prefix family collapses to the
    same wire identifier), so fold a per-invocation discriminator BEFORE the
    run-id suffix and truncate the base after reserving room for both.
    """
    base = re.sub(r"[^a-z0-9-]+", "-", username.lower()).strip("-")
    if not base or not base[0].isalpha():
        base = f"isv-{base}".strip("-")
    run_suffix = unique_suffix("x", length=8).split("-", 1)[-1]  # 8 hex chars
    disc = secrets.token_hex(2)  # 4 hex chars, fresh per invocation
    # Reserve "-<disc>-<run_suffix>" so the assembled ID fits in 30 chars.
    reserve = len(disc) + len(run_suffix) + 2
    base = base[: _SA_ID_MAX_LEN - reserve].rstrip("-") or "isv"
    return f"{base}-{disc}-{run_suffix}"[:_SA_ID_MAX_LEN]


def _grant_token_creator(iam: iam_admin_v1.IAMClient, sa_email: str) -> None:
    """Grant the ADC principal tokenCreator on the new SA's resource policy.

    Read-modify-write the service account's own IAM policy (carrying its etag)
    rather than overwriting it, so the binding is added without clobbering any
    policy the create call may have seeded. The read-modify-write is wrapped in
    a bounded retry that re-reads on each attempt (fresh etag), matching the
    sibling token-mint path's transient-shape resilience: a 5xx / 429 or a
    stale-etag 409 on the bind otherwise aborts the whole create + cleanup +
    re-provision cycle.
    """
    member = resolve_principal_member()
    resource = f"projects/-/serviceAccounts/{sa_email}"

    def _read() -> Any:
        return iam.get_iam_policy(request=iam_policy_pb2.GetIamPolicyRequest(resource=resource))

    def _mutate(policy: Any) -> None:
        policy.bindings.append(policy_pb2.Binding(role=_TOKEN_CREATOR_ROLE, members=[member]))

    def _write(policy: Any) -> None:
        iam.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy))

    modify_iam_policy_with_retry(_read, _write, _mutate, resource_desc=f"service account {sa_email}")


def _mint_access_token(sa_email: str) -> tuple[str, str]:
    """Mint a short-lived impersonation token, retrying while the binding propagates.

    Returns ``(access_token, rfc3339_expiry)``. The retry envelope lives here
    (not in test_credentials) because the tokenCreator-binding propagation
    happens during create-time impersonation. Only the propagation /
    transient shapes are retried; any other error re-raises immediately so a
    malformed request fails fast.
    """
    creds_client = iam_credentials_v1.IAMCredentialsClient()
    name = f"projects/-/serviceAccounts/{sa_email}"
    lifetime = duration_pb2.Duration(seconds=TOKEN_LIFETIME_SECONDS)

    last_error: str | None = None
    for attempt in range(1, TOKEN_MINT_MAX_RETRIES + 1):
        try:
            response = creds_client.generate_access_token(
                name=name,
                scope=[_CLOUD_PLATFORM_SCOPE],
                lifetime=lifetime,
            )
        except _RETRYABLE_MINT as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < TOKEN_MINT_MAX_RETRIES:
                print(
                    f"  generate_access_token attempt {attempt}/{TOKEN_MINT_MAX_RETRIES} "
                    f"({type(e).__name__}); tokenCreator binding propagating, "
                    f"retrying in {TOKEN_MINT_RETRY_DELAY_SECONDS}s",
                    file=sys.stderr,
                )
                time.sleep(TOKEN_MINT_RETRY_DELAY_SECONDS)
                continue
            break
        # proto-plus exposes the Timestamp field as a datetime at runtime;
        # widen to Any so the isoformat call type-checks against the proto stub.
        expiry: Any = response.expire_time
        rfc3339 = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)
        return response.access_token, rfc3339

    budget = TOKEN_MINT_MAX_RETRIES * TOKEN_MINT_RETRY_DELAY_SECONDS
    msg = f"generate_access_token did not converge within {budget}s: {last_error}"
    raise RuntimeError(msg)


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Create a GCP service account + impersonation token")
    parser.add_argument("--username", default="isv-test-user", help="Service account ID base")
    parser.add_argument(
        "--create-access-key",
        action="store_true",
        default=False,
        help="Mint short-lived credential material for test_credentials",
    )
    parser.add_argument("--project", default="", help="GCP project (falls back to env/ADC when blank)")
    args = parser.parse_args()

    project = resolve_project(args.project or None)
    account_id = _service_account_id(args.username)

    result: dict = {
        "success": False,
        "platform": "iam",
        "project": project,
    }

    iam = iam_admin_v1.IAMClient()
    sa_email: str | None = None
    try:
        service_account = iam_admin_v1.ServiceAccount(display_name="isvtest IAM lifecycle validation")
        created = iam.create_service_account(
            name=f"projects/{project}",
            account_id=account_id,
            service_account=service_account,
        )
        sa_email = created.email
        result["username"] = created.email
        result["user_id"] = created.unique_id
        result["service_account_name"] = created.name
        # NON-SECRET: the service account unique_id. test_credentials compares
        # this to tokeninfo.azp to prove the token is the expected identity.
        result["access_key_id"] = created.unique_id

        if args.create_access_key:
            _grant_token_creator(iam, sa_email)
            token, token_expiry = _mint_access_token(sa_email)
            result["secret_access_key"] = token
            result["token_expiry"] = token_expiry

        result["success"] = True
    except Exception as e:
        # Structured failure + best-effort cleanup of the partial identity.
        error_type, error_msg = classify_gcp_error(e)
        result["error_type"] = error_type
        result["error"] = error_msg
        # Delete the partially-created identity so a mint/bind failure does not
        # leak a project-level service account.
        if sa_email:
            result["cleanup"] = {"service_account_deleted": delete_service_account(sa_email, project=project)}
        result["success"] = False
        print(json.dumps(result, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
