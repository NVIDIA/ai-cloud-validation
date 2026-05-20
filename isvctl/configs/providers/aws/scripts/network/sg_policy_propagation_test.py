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

"""Measure security policy propagation timing through AWS security groups.

AWS exposes tenant network-filter policy as EC2 security group rules. This
probe creates a temporary SG in the target VPC, adds one inbound rule, polls
until ``DescribeSecurityGroups`` reports the rule, revokes it, then polls until
the rule disappears. The validation consumes only the resulting JSON timings.

Usage:
    python sg_policy_propagation_test.py --region us-west-2 --vpc-id vpc-xxx
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.errors import delete_with_retry, handle_aws_errors

TEST_NAMES = [
    "create_probe_rule",
    "rule_observed",
    "revoke_probe_rule",
    "removal_observed",
    "cleanup",
]
DEFAULT_MAX_PROPAGATION_SECONDS = 10.0
DEFAULT_POLL_SECONDS = 0.5


def _passed(message: str = "", **extra: Any) -> dict[str, Any]:
    """Return a passing subtest result."""
    result: dict[str, Any] = {"passed": True}
    if message:
        result["message"] = message
    result.update(extra)
    return result


def _failed(error: str, **extra: Any) -> dict[str, Any]:
    """Return a failing subtest result."""
    result: dict[str, Any] = {"passed": False, "error": error}
    result.update(extra)
    return result


def _base_result(max_propagation_seconds: float) -> dict[str, Any]:
    """Build the result envelope with subtests initialised to failed."""
    return {
        "success": False,
        "platform": "network",
        "test_name": "sg_policy_propagation",
        "max_propagation_seconds": max_propagation_seconds,
        "tests": {name: {"passed": False} for name in TEST_NAMES},
    }


def _probe_permission() -> list[dict[str, Any]]:
    """Return the temporary security group permission used for timing."""
    return [
        {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
        }
    ]


def _permission_present(security_group: dict[str, Any], permission: list[dict[str, Any]]) -> bool:
    """Return whether the probe permission is visible in a described SG."""
    expected = permission[0]
    expected_cidrs = {ip_range["CidrIp"] for ip_range in expected.get("IpRanges", [])}
    for rule in security_group.get("IpPermissions", []):
        if (
            rule.get("IpProtocol") != expected["IpProtocol"]
            or rule.get("FromPort") != expected["FromPort"]
            or rule.get("ToPort") != expected["ToPort"]
        ):
            continue
        actual_cidrs = {ip_range.get("CidrIp") for ip_range in rule.get("IpRanges", [])}
        if expected_cidrs <= actual_cidrs:
            return True
    return False


def _wait_for_permission_state(
    ec2: Any,
    sg_id: str,
    permission: list[dict[str, Any]],
    *,
    expected_present: bool,
    max_seconds: float,
    poll_seconds: float,
) -> tuple[bool, float]:
    """Poll ``DescribeSecurityGroups`` until the probe permission reaches the expected state."""
    start = time.monotonic()
    while True:
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        groups = response.get("SecurityGroups", [])
        present = bool(groups) and _permission_present(groups[0], permission)
        elapsed = time.monotonic() - start
        if present is expected_present:
            return True, elapsed
        if elapsed >= max_seconds:
            return False, elapsed
        time.sleep(poll_seconds)


def check_policy_propagation(
    ec2: Any,
    vpc_id: str,
    region: str,
    *,
    max_propagation_seconds: float = DEFAULT_MAX_PROPAGATION_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> dict[str, Any]:
    """Measure SG rule add/remove propagation timing."""
    _ = region
    result = _base_result(max_propagation_seconds)
    permission = _probe_permission()
    sg_id = None
    sg_name = f"isv-sdn-policy-propagation-{uuid.uuid4().hex[:8]}"
    failed_key = "create_probe_rule"

    try:
        sg = ec2.create_security_group(
            GroupName=sg_name,
            Description="ISV policy propagation probe",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": sg_name},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        sg_id = sg["GroupId"]
        result["target_rule_id"] = sg_id

        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=permission)
        result["tests"]["create_probe_rule"] = _passed("Probe rule added")

        failed_key = "rule_observed"
        observed, add_seconds = _wait_for_permission_state(
            ec2,
            sg_id,
            permission,
            expected_present=True,
            max_seconds=max_propagation_seconds,
            poll_seconds=poll_seconds,
        )
        result["add_observed_seconds"] = round(add_seconds, 3)
        if not observed:
            result["tests"]["rule_observed"] = _failed(
                f"Probe rule was not observable within {max_propagation_seconds:.2f}s",
                propagation_timeout=True,
                seconds=round(add_seconds, 3),
            )
            return result
        result["tests"]["rule_observed"] = _passed(
            f"Probe rule observable after {add_seconds:.2f}s",
            seconds=round(add_seconds, 3),
        )

        failed_key = "revoke_probe_rule"
        ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=permission)
        result["tests"]["revoke_probe_rule"] = _passed("Probe rule revoked")

        failed_key = "removal_observed"
        removed, remove_seconds = _wait_for_permission_state(
            ec2,
            sg_id,
            permission,
            expected_present=False,
            max_seconds=max_propagation_seconds,
            poll_seconds=poll_seconds,
        )
        result["remove_observed_seconds"] = round(remove_seconds, 3)
        if not removed:
            result["tests"]["removal_observed"] = _failed(
                f"Probe rule removal was not observable within {max_propagation_seconds:.2f}s",
                propagation_timeout=True,
                seconds=round(remove_seconds, 3),
            )
            return result
        result["tests"]["removal_observed"] = _passed(
            f"Probe rule removal observable after {remove_seconds:.2f}s",
            seconds=round(remove_seconds, 3),
        )

    except ClientError as e:
        error_type = e.response.get("Error", {}).get("Code", "ClientError")
        result["tests"][failed_key] = _failed("Provider API call failed", error_type=error_type)
        result["error"] = "Provider API call failed"
        result["error_type"] = error_type
    finally:
        if sg_id:
            deleted = delete_with_retry(
                ec2.delete_security_group,
                GroupId=sg_id,
                resource_desc=f"policy propagation probe security group {sg_id}",
            )
            if deleted:
                result["tests"]["cleanup"] = _passed("Probe rule cleanup completed")
            else:
                result["tests"]["cleanup"] = _failed("Probe rule cleanup failed")
        else:
            result["tests"]["cleanup"] = _passed("No probe rule was created")

        result["success"] = all(test.get("passed") for test in result["tests"].values())
        if not result["success"] and "error" not in result:
            result["error"] = "Security policy propagation timing checks failed"

    return result


@handle_aws_errors
def main() -> int:
    """Run the AWS policy propagation timing probe."""
    parser = argparse.ArgumentParser(description="Measure security policy propagation timing")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--vpc-id", required=True, help="Target VPC/network identifier")
    parser.add_argument(
        "--max-propagation-seconds",
        type=float,
        default=DEFAULT_MAX_PROPAGATION_SECONDS,
        help="Maximum acceptable add/remove propagation time",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Polling interval while waiting for policy state changes",
    )
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    result = check_policy_propagation(
        ec2,
        args.vpc_id,
        args.region,
        max_propagation_seconds=args.max_propagation_seconds,
        poll_seconds=args.poll_seconds,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
