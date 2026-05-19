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

"""Verify the platform issues short-lived credentials to nodes and workloads (SEC02-01).

This AWS reference is self-contained (mirrors ``sa_credential_test.py``):
it provisions a fresh IAM user with an inline policy granting just the two
STS issuance APIs we probe, mints an access key, runs the probes against
that user, and cleans the user up in a ``finally`` block. This means the
test runs successfully even when the orchestrator principal is itself an
assumed-role/SSO session - those callers cannot invoke
``sts:GetSessionToken``/``sts:GetFederationToken`` directly, but they can
``iam:CreateUser`` and let the test impersonate a fresh IAM user.

Two STS issuance paths are probed; their response shape mirrors the node
and workload identity flows the requirement targets:

* Node-equivalent: ``sts:GetSessionToken`` -- the API a long-lived IAM
  user uses to mint short-lived session credentials, equivalent in shape
  to the credentials an EC2 instance receives via instance metadata.
* Workload-equivalent: ``sts:GetFederationToken`` (with a deny-all session
  policy) -- the API used to issue short-lived credentials to an external
  workload identity, equivalent in shape to the credentials an
  IRSA-enabled pod receives.

Each probe asserts the returned credential carries a finite expiry that
does not exceed a configured upper bound.

When the orchestrator principal cannot provision the test IAM user (e.g.
read-only credentials), the script emits a structured ``skipped`` payload
(exit 0) so the validation can skip rather than fabricate a pass.

Required orchestrator-principal IAM permissions:
    iam:CreateUser, iam:PutUserPolicy, iam:CreateAccessKey,
    iam:DeleteUserPolicy, iam:DeleteAccessKey, iam:DeleteUser

Usage:
    python short_lived_credentials_test.py --region us-west-2

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "short_lived_credentials_test",
    "node_credential_method": "sts:GetSessionToken",
    "workload_credential_method": "sts:GetFederationToken",
    "node_credential_ttl_seconds": 43197,
    "workload_credential_ttl_seconds": 43197,
    "max_ttl_seconds": 43200,
    "tests": {
      "node_credential_has_expiry":           {"passed": true},
      "node_credential_ttl_within_bound":     {"passed": true},
      "workload_credential_has_expiry":       {"passed": true},
      "workload_credential_ttl_within_bound": {"passed": true}
    }
  }
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors

DEFAULT_MAX_TTL_SECONDS = 43200  # 12h - DGXC SEC02 upper bound for short-lived creds
NODE_METHOD = "sts:GetSessionToken"
WORKLOAD_METHOD = "sts:GetFederationToken"
# Federation Name + IAM username share a per-run suffix so CloudTrail /
# IAM Access Analyzer events from the same probe correlate cleanly.
WORKLOAD_FEDERATION_PREFIX = "isv-sec02-workload-"
TEST_USER_PREFIX = "isv-sec02-test-"
INLINE_POLICY_NAME = "isv-sec02-sts-allow"
DENY_ALL_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Deny", "Action": "*", "Resource": "*"}],
    }
)
INLINE_STS_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["sts:GetSessionToken", "sts:GetFederationToken"],
                "Resource": "*",
            }
        ],
    }
)
# AWS error codes that mean the orchestrator principal cannot provision
# the test IAM user, which is operational signal (not a SEC02 failure)
# -> emit a structured skip.
SKIPPABLE_SETUP_ERRORS = frozenset({"AccessDenied", "UnauthorizedOperation"})

# IAM is eventually consistent: a freshly created access key may take
# 15-30s to propagate to STS. Mirror sa_credential_test.py's retry
# pattern: exponential backoff capped at 8s, 8 attempts (~46s worst case).
STS_PROPAGATION_MAX_ATTEMPTS = 8
STS_PROPAGATION_BACKOFF_CAP = 8


def _ttl_seconds(expiration: datetime) -> int:
    """Return seconds until ``expiration``, treating naive datetimes as UTC."""
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return int((expiration - now).total_seconds())


def _skipped_result(reason: str) -> dict[str, Any]:
    """Return a structured top-level skip payload for the validation."""
    return {
        "success": True,
        "platform": "security",
        "test_name": "short_lived_credentials_test",
        "skipped": True,
        "skip_reason": reason,
        "tests": {},
    }


def _record_credential(
    result: dict[str, Any],
    *,
    expiry_key: str,
    ttl_key: str,
    ttl_field: str,
    expiration: datetime | None,
    max_ttl_seconds: int,
) -> None:
    """Update ``result`` with expiry + TTL probe outcomes for one credential."""
    if expiration is None:
        result["tests"][expiry_key]["error"] = "Credentials.Expiration missing in STS response"
        result["tests"][ttl_key]["error"] = "no expiry returned -- TTL bound cannot be evaluated"
        return
    result["tests"][expiry_key]["passed"] = True
    ttl = _ttl_seconds(expiration)
    result[ttl_field] = ttl
    if 0 < ttl <= max_ttl_seconds:
        result["tests"][ttl_key]["passed"] = True
    else:
        result["tests"][ttl_key]["error"] = f"TTL {ttl}s outside (0, {max_ttl_seconds}s] bound"


def _cleanup_test_user(
    iam: Any,
    username: str | None,
    access_key_id: str | None,
    user_created: bool,
) -> list[str]:
    """Best-effort delete of the test IAM user's policy, access key, and user.

    Each step is attempted independently so a failure in one does not
    block the others. ``NoSuchEntity`` is treated as success (already gone).
    """
    cleanup_errors: list[str] = []
    if not username:
        return cleanup_errors

    if user_created:
        try:
            iam.delete_user_policy(UserName=username, PolicyName=INLINE_POLICY_NAME)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
                cleanup_errors.append(f"delete inline policy {INLINE_POLICY_NAME} for {username}: {e}")

    if access_key_id:
        try:
            iam.delete_access_key(UserName=username, AccessKeyId=access_key_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
                cleanup_errors.append(f"delete access key {access_key_id} for {username}: {e}")

    if user_created:
        try:
            iam.delete_user(UserName=username)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
                cleanup_errors.append(f"delete user {username}: {e}")

    return cleanup_errors


def _probe_node_credential(sts: Any) -> datetime | None:
    """Call sts:GetSessionToken with retries for IAM eventual consistency.

    Raises ``ClientError`` if all retry attempts are exhausted or the
    error is not retryable.
    """
    for attempt in range(STS_PROPAGATION_MAX_ATTEMPTS):
        try:
            response = sts.get_session_token()
            return response.get("Credentials", {}).get("Expiration")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "InvalidClientTokenId" and attempt < STS_PROPAGATION_MAX_ATTEMPTS - 1:
                time.sleep(min(2 ** (attempt + 1), STS_PROPAGATION_BACKOFF_CAP))
                continue
            raise
    return None


def _probe_workload_credential(sts: Any, federation_name: str) -> datetime | None:
    """Call sts:GetFederationToken with a deny-all session policy."""
    response = sts.get_federation_token(
        Name=federation_name,
        Policy=DENY_ALL_POLICY,
    )
    return response.get("Credentials", {}).get("Expiration")


@handle_aws_errors
def main() -> int:
    """Provision a test IAM user, probe STS issuance, emit JSON, clean up."""
    parser = argparse.ArgumentParser(description="Short-lived credentials test (SEC02-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--max-ttl-seconds",
        type=int,
        default=DEFAULT_MAX_TTL_SECONDS,
        help=f"Upper bound on credential TTL (default: {DEFAULT_MAX_TTL_SECONDS})",
    )
    args = parser.parse_args()

    if args.max_ttl_seconds < 1:
        print(json.dumps(_skipped_result("--max-ttl-seconds must be a positive integer"), indent=2))
        return 0

    iam = boto3.client("iam", region_name=args.region)

    # Setup runs as three separate IAM calls. Track partial state so the
    # cleanup helper can roll back whatever did succeed when any step
    # raises (e.g. CreateUser succeeds but PutUserPolicy hits LimitExceeded
    # or AccessDenied -> the user was created and must be deleted).
    run_suffix = uuid.uuid4().hex[:8]
    username = f"{TEST_USER_PREFIX}{run_suffix}"
    federation_name = f"{WORKLOAD_FEDERATION_PREFIX}{run_suffix}"
    access_key_id: str | None = None
    secret_key: str | None = None
    user_created = False

    try:
        iam.create_user(UserName=username, Tags=[{"Key": "CreatedBy", "Value": "isvtest"}])
        user_created = True
        iam.put_user_policy(
            UserName=username,
            PolicyName=INLINE_POLICY_NAME,
            PolicyDocument=INLINE_STS_POLICY,
        )
        response = iam.create_access_key(UserName=username)
        access_key_id = response["AccessKey"]["AccessKeyId"]
        secret_key = response["AccessKey"]["SecretAccessKey"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        cleanup_errors = _cleanup_test_user(iam, username, access_key_id, user_created)
        if cleanup_errors:
            # Even if the setup error itself is skippable, a failed cleanup
            # means we leaked IAM state. Surface as a hard failure so the
            # run is not reported as a clean skip.
            print(
                json.dumps(
                    {
                        "success": False,
                        "platform": "security",
                        "test_name": "short_lived_credentials_test",
                        "error": f"setup failed: {exc}; cleanup failed: {'; '.join(cleanup_errors)}",
                        "cleanup_errors": cleanup_errors,
                        "tests": {},
                    },
                    indent=2,
                )
            )
            return 1
        if code in SKIPPABLE_SETUP_ERRORS:
            print(
                json.dumps(
                    _skipped_result(
                        f"cannot provision SEC02 test IAM user: {exc}; "
                        "orchestrator principal needs iam:CreateUser, iam:PutUserPolicy, "
                        "iam:CreateAccessKey (and matching delete permissions for cleanup)"
                    ),
                    indent=2,
                )
            )
            return 0
        raise

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "short_lived_credentials_test",
        "node_credential_method": NODE_METHOD,
        "workload_credential_method": WORKLOAD_METHOD,
        "node_credential_ttl_seconds": 0,
        "workload_credential_ttl_seconds": 0,
        "max_ttl_seconds": args.max_ttl_seconds,
        "tests": {
            "node_credential_has_expiry": {"passed": False},
            "node_credential_ttl_within_bound": {"passed": False},
            "workload_credential_has_expiry": {"passed": False},
            "workload_credential_ttl_within_bound": {"passed": False},
        },
    }

    try:
        sts = boto3.client(
            "sts",
            region_name=args.region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_key,
        )

        node_expiration = _probe_node_credential(sts)
        _record_credential(
            result,
            expiry_key="node_credential_has_expiry",
            ttl_key="node_credential_ttl_within_bound",
            ttl_field="node_credential_ttl_seconds",
            expiration=node_expiration,
            max_ttl_seconds=args.max_ttl_seconds,
        )

        try:
            workload_expiration = _probe_workload_credential(sts, federation_name)
        except ClientError as exc:
            error_msg = f"{WORKLOAD_METHOD} failed: {exc}"
            result["tests"]["workload_credential_has_expiry"]["error"] = error_msg
            result["tests"]["workload_credential_ttl_within_bound"]["error"] = error_msg
        else:
            _record_credential(
                result,
                expiry_key="workload_credential_has_expiry",
                ttl_key="workload_credential_ttl_within_bound",
                ttl_field="workload_credential_ttl_seconds",
                expiration=workload_expiration,
                max_ttl_seconds=args.max_ttl_seconds,
            )

        result["success"] = all(probe["passed"] for probe in result["tests"].values())
    finally:
        cleanup_errors = _cleanup_test_user(iam, username, access_key_id, user_created)
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            cleanup_msg = f"Cleanup failed: {'; '.join(cleanup_errors)}"
            existing = result.get("error")
            result["error"] = f"{existing}; {cleanup_msg}" if existing else cleanup_msg
            result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
