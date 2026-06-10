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

"""Verify all administrative interfaces (UI, CLI, API) are protected by MFA (AWS reference).

Tests that the AWS account enforces Multi-Factor Authentication:

  1. admin_account_mfa:        Root / admin account has an MFA device attached
     (AccountMFAEnabled from GetAccountSummary).
  2. interactive_access_mfa:   Every IAM user with a console login password has
     at least one MFA device registered.
  3. programmatic_access_mfa:  At least one attached customer-managed IAM policy
     contains an explicit Deny-without-MFA enforcement pattern
     (``Effect: Deny`` + ``BoolIfExists aws:MultiFactorAuthPresent = false``).
     The pattern is transport-agnostic, so a matching policy covers both API-
     and CLI-initiated access in a single check. This asserts that an MFA-deny
     control *exists*; because IAM evaluates policies per principal/request, it
     does not by itself prove every programmatic principal is in scope of that
     deny (verifying per-principal coverage would require simulating each
     principal, which this lightweight check does not do).

Usage:
    python mfa_enforcement_test.py --region us-west-2

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "mfa_enforcement",
    "interfaces_checked": 3,
    "tests": { ... }
  }
"""

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors


def _check_root_mfa(iam: Any) -> dict[str, Any]:
    """Verify the root account has MFA enabled."""
    try:
        summary = iam.get_account_summary()["SummaryMap"]
        if summary.get("AccountMFAEnabled", 0) == 1:
            return {"passed": True, "message": "Root account MFA is enabled"}
        return {"passed": False, "error": "Root account MFA is NOT enabled"}
    except ClientError as e:
        return {"passed": False, "error": str(e)}


def _check_console_users_mfa(iam: Any) -> dict[str, Any]:
    """Verify every IAM user with a console password has MFA attached."""
    try:
        paginator = iam.get_paginator("list_users")
        users_without_mfa: list[str] = []
        console_user_count = 0

        for page in paginator.paginate():
            for user in page["Users"]:
                username = user["UserName"]
                try:
                    iam.get_login_profile(UserName=username)
                except ClientError as e:
                    if e.response["Error"]["Code"] == "NoSuchEntity":
                        continue
                    raise

                console_user_count += 1
                mfa_devices = iam.list_mfa_devices(UserName=username)["MFADevices"]
                if not mfa_devices:
                    users_without_mfa.append(username)

        if not console_user_count:
            return {"passed": True, "message": "No IAM console users (programmatic-only)"}

        if users_without_mfa:
            return {
                "passed": False,
                "error": (f"{len(users_without_mfa)}/{console_user_count} console users lack MFA: {users_without_mfa}"),
            }

        return {
            "passed": True,
            "message": f"{console_user_count}/{console_user_count} console users have MFA",
        }
    except ClientError as e:
        return {"passed": False, "error": str(e)}


def _has_mfa_deny_enforcement(policy_document: dict[str, Any]) -> bool:
    """Return True if the policy contains a Deny-without-MFA enforcement pattern.

    Per AWS best practices, MFA enforcement requires explicit Deny statements
    that block access when MFA is absent — e.g.:
        Effect: Deny + Condition: BoolIfExists aws:MultiFactorAuthPresent = false

    An Allow with an MFA condition is NOT sufficient because other policies
    can grant unconditional access that bypasses the MFA gate.
    """
    DENY_OPERATORS = {"BoolIfExists"}
    DENY_VALUES = {"false", False}

    statements = policy_document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]

    for stmt in statements:
        if str(stmt.get("Effect", "")).lower() != "deny":
            continue
        condition = stmt.get("Condition", {})
        for operator, condition_block in condition.items():
            if operator not in DENY_OPERATORS or not isinstance(condition_block, dict):
                continue
            mfa_val = condition_block.get("aws:MultiFactorAuthPresent")
            if mfa_val in DENY_VALUES:
                return True
            age_val = condition_block.get("aws:MultiFactorAuthAge")
            if age_val is not None:
                return True
    return False


def _check_mfa_policy(iam: Any, label: str) -> dict[str, Any]:
    """Scan account/customer-managed policies for MFA-enforcement conditions.

    Returns the result for the programmatic_access_mfa subtest. The MFA-deny
    condition is transport-agnostic, so a matching policy covers both API- and
    CLI-initiated programmatic access in one check. This passes when such a
    policy *exists*; it does not verify per-principal coverage (IAM evaluates
    policies per request, so an existence check cannot prove every programmatic
    principal is denied without MFA).
    """
    try:
        import urllib.parse

        paginator = iam.get_paginator("list_policies")
        mfa_policies: list[str] = []

        for page in paginator.paginate(Scope="Local", OnlyAttached=True):
            for policy in page["Policies"]:
                arn = policy["Arn"]
                version_id = policy["DefaultVersionId"]
                try:
                    doc_response = iam.get_policy_version(PolicyArn=arn, VersionId=version_id)
                    doc = doc_response["PolicyVersion"]["Document"]
                    if isinstance(doc, str):
                        doc = json.loads(urllib.parse.unquote(doc))
                    if _has_mfa_deny_enforcement(doc):
                        mfa_policies.append(policy["PolicyName"])
                except ClientError:
                    continue

        if mfa_policies:
            return {
                "passed": True,
                "message": f"MFA condition found in {label} policies: {mfa_policies}",
            }

        return {
            "passed": False,
            "error": f"No attached customer-managed policies enforce MFA for {label} access",
        }
    except ClientError as e:
        return {"passed": False, "error": str(e)}


@handle_aws_errors
def main() -> int:
    """Run MFA enforcement checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="MFA enforcement test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    iam = boto3.client("iam", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "mfa_enforcement",
        "interfaces_checked": 0,
        "tests": {},
    }

    result["tests"]["admin_account_mfa"] = _check_root_mfa(iam)
    result["tests"]["interactive_access_mfa"] = _check_console_users_mfa(iam)

    # The MFA-deny condition is transport-agnostic (applies to both API- and
    # CLI-initiated calls), so one policy check covers programmatic access. This
    # asserts an MFA-deny policy exists, not that every principal is in scope.
    result["tests"]["programmatic_access_mfa"] = _check_mfa_policy(iam, "programmatic")

    result["interfaces_checked"] = len(result["tests"])
    result["success"] = all(t.get("passed") for t in result["tests"].values())

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
