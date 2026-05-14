#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Validate EC2 serial console RBAC using IAM principal policy simulation.

The EC2 serial console SSH path is authorized by
``ec2-instance-connect:SendSerialConsoleSSHPublicKey``. This script verifies
account-level serial-console availability, then creates temporary IAM test
principals and simulates their actual attached policies. The denied principal
has no console policy, while the allowed principal has a scoped inline policy
for the target instance only.

Usage:
    python console_rbac.py --instance-id i-xxx --region us-west-2
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
import botocore.session
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors
from common.serial_console import check_serial_access

CONSOLE_ACTION = "ec2-instance-connect:SendSerialConsoleSSHPublicKey"
TEST_USER_PREFIX = "isv-console-rbac-test-"
ALLOW_POLICY_NAME = "IsvConsoleRbacScopedAccess"
OWNER_TAG = {"Key": "CreatedBy", "Value": "isvtest"}
SERIAL_ACCESS_DISABLED_SKIP_REASON = "EC2 serial console access is disabled for this account or region"
REQUIRED_TESTS = (
    "serial_console_access_enabled",
    "denied_principal_cannot_access_console",
    "allowed_principal_can_access_console",
    "allowed_principal_is_resource_scoped",
)


def _partition_from_identity(identity: dict[str, Any], region: str) -> str:
    """Derive the AWS partition from STS identity, falling back to region metadata."""
    arn = identity.get("Arn", "")
    if arn.startswith("arn:"):
        parts = arn.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    try:
        partition = botocore.session.get_session().get_partition_for_region(region)
    except Exception:
        return "aws"
    return str(partition or "aws")


def _instance_arn(partition: str, region: str, account_id: str, instance_id: str) -> str:
    """Build an EC2 instance ARN for IAM simulation."""
    return f"arn:{partition}:ec2:{region}:{account_id}:instance/{instance_id}"


def _iam_user_arn(partition: str, account_id: str, username: str) -> str:
    """Build a default-path IAM user ARN for a temporary test principal."""
    return f"arn:{partition}:iam::{account_id}:user/{username}"


def _other_instance_id(instance_id: str) -> str:
    """Return a different well-formed EC2 instance ID for scoping simulation."""
    candidate = "i-00000000000000000"
    if instance_id == candidate:
        return "i-11111111111111111"
    return candidate


def _make_test_result(passed: bool, **details: Any) -> dict[str, Any]:
    """Build a standard test result dictionary."""
    return {"passed": passed, **details}


def _mark_skipped(result: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark the whole RBAC probe as skipped with skipped subtest evidence."""
    result["success"] = True
    result["skipped"] = True
    result["skip_reason"] = reason
    result["tests"] = {
        name: {
            "passed": True,
            "skipped": True,
            "skip_reason": reason,
        }
        for name in REQUIRED_TESTS
    }
    return result


def _policy_document(statements: list[dict[str, Any]]) -> str:
    """Return a JSON IAM policy document for simulation."""
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def _allow_console_policy(instance_arn: str) -> str:
    """Return an inline IAM policy allowing console access to one instance."""
    return _policy_document(
        [
            {
                "Effect": "Allow",
                "Action": CONSOLE_ACTION,
                "Resource": instance_arn,
            }
        ]
    )


def _create_test_user(iam: Any, username: str, partition: str, account_id: str) -> str:
    """Create one tagged IAM user and return its ARN."""
    response = iam.create_user(UserName=username, Tags=[OWNER_TAG])
    return str(response.get("User", {}).get("Arn") or _iam_user_arn(partition, account_id, username))


def _simulate_principal_console_decision(iam: Any, principal_arn: str, instance_arn: str) -> str:
    """Simulate console-access authorization for an actual IAM principal."""
    response = iam.simulate_principal_policy(
        PolicySourceArn=principal_arn,
        ActionNames=[CONSOLE_ACTION],
        ResourceArns=[instance_arn],
    )
    results = response.get("EvaluationResults", [])
    if not results:
        msg = "IAM simulation returned no evaluation results"
        raise RuntimeError(msg)
    return str(results[0].get("EvalDecision", ""))


def _cleanup_test_users(iam: Any, created_policies: dict[str, set[str]], created_users: list[str]) -> list[str]:
    """Delete inline policies and IAM users created by this script."""
    cleanup_errors: list[str] = []

    for username in reversed(created_users):
        policy_names = set(created_policies.get(username, set()))
        try:
            response = iam.list_user_policies(UserName=username)
            policy_names.update(str(name) for name in response.get("PolicyNames", []))
        except ClientError as e:
            cleanup_errors.append(f"list inline policies for {username}: {e}")

        for policy_name in sorted(policy_names):
            try:
                iam.delete_user_policy(UserName=username, PolicyName=policy_name)
            except ClientError as e:
                cleanup_errors.append(f"delete inline policy {policy_name} for {username}: {e}")

        try:
            iam.delete_user(UserName=username)
        except ClientError as e:
            cleanup_errors.append(f"delete user {username}: {e}")

    return cleanup_errors


def _is_allowed(decision: str) -> bool:
    """Return True when the IAM simulation decision allows access."""
    return decision.lower() == "allowed"


def _run_console_rbac_check(iam: Any, sts: Any, ec2: Any, instance_id: str, region: str) -> dict[str, Any]:
    """Run the console RBAC policy simulation workflow."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "test_name": "console_rbac",
        "instance_id": instance_id,
        "serial_access_enabled": False,
        "rbac_model": "aws-iam-principal-policy",
        "access_restricted": False,
        "restricted_actions": [CONSOLE_ACTION],
        "tests": {},
    }

    created_users: list[str] = []
    created_policies: dict[str, set[str]] = {}

    try:
        serial_access = check_serial_access(ec2)
        serial_access_enabled = serial_access.get("enabled") is True
        result["serial_access_enabled"] = serial_access_enabled
        result["tests"]["serial_console_access_enabled"] = _make_test_result(serial_access_enabled)
        if serial_access.get("error"):
            result["tests"]["serial_console_access_enabled"]["error"] = serial_access["error"]
            result["error"] = serial_access["error"]
            return result
        if not serial_access_enabled:
            return _mark_skipped(result, SERIAL_ACCESS_DISABLED_SKIP_REASON)

        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        partition = _partition_from_identity(identity, region)
        target_arn = _instance_arn(partition, region, account_id, instance_id)
        other_arn = _instance_arn(partition, region, account_id, _other_instance_id(instance_id))
        scoped_allow_policy = _allow_console_policy(target_arn)
        suffix = uuid.uuid4().hex[:8]
        denied_username = f"{TEST_USER_PREFIX}denied-{suffix}"
        allowed_username = f"{TEST_USER_PREFIX}allowed-{suffix}"
        denied_principal_arn = _create_test_user(iam, denied_username, partition, account_id)
        created_users.append(denied_username)
        allowed_principal_arn = _create_test_user(iam, allowed_username, partition, account_id)
        created_users.append(allowed_username)
        result["principals"] = {
            "denied": denied_principal_arn,
            "allowed": allowed_principal_arn,
        }

        denied_decision = _simulate_principal_console_decision(iam, denied_principal_arn, target_arn)
        denied_passed = not _is_allowed(denied_decision)
        result["tests"]["denied_principal_cannot_access_console"] = _make_test_result(
            denied_passed,
            principal=denied_principal_arn,
            decision=denied_decision,
        )

        iam.put_user_policy(
            UserName=allowed_username,
            PolicyName=ALLOW_POLICY_NAME,
            PolicyDocument=scoped_allow_policy,
        )
        created_policies.setdefault(allowed_username, set()).add(ALLOW_POLICY_NAME)

        allowed_decision = _simulate_principal_console_decision(iam, allowed_principal_arn, target_arn)
        allowed_passed = _is_allowed(allowed_decision) and serial_access_enabled
        result["tests"]["allowed_principal_can_access_console"] = _make_test_result(
            allowed_passed,
            principal=allowed_principal_arn,
            decision=allowed_decision,
            resource=target_arn,
            serial_access_enabled=serial_access_enabled,
        )
        if not serial_access_enabled:
            result["tests"]["allowed_principal_can_access_console"]["error"] = (
                "IAM allows the principal, but EC2 serial console access is disabled"
            )

        scoped_decision = _simulate_principal_console_decision(iam, allowed_principal_arn, other_arn)
        scoped_passed = not _is_allowed(scoped_decision)
        result["tests"]["allowed_principal_is_resource_scoped"] = _make_test_result(
            scoped_passed,
            principal=allowed_principal_arn,
            decision=scoped_decision,
            resource=other_arn,
        )

        result["access_restricted"] = denied_passed and scoped_passed
        result["success"] = all(result["tests"].get(name, {}).get("passed") is True for name in REQUIRED_TESTS)
    except Exception as e:
        result["error"] = str(e)
    finally:
        cleanup_errors = _cleanup_test_users(iam, created_policies, created_users)
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            cleanup_error = f"Cleanup failed: {'; '.join(cleanup_errors)}"
            result["error"] = f"{result['error']}; {cleanup_error}" if result.get("error") else cleanup_error
            result["success"] = False

    return result


@handle_aws_errors
def main() -> int:
    """Validate console RBAC and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Validate EC2 serial console RBAC")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    iam = boto3.client("iam", region_name=args.region)
    sts = boto3.client("sts", region_name=args.region)
    ec2 = boto3.client("ec2", region_name=args.region)

    result = _run_console_rbac_check(iam, sts, ec2, args.instance_id, args.region)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
