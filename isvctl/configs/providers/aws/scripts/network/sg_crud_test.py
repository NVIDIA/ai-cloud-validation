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

"""Test Security Group CRUD operations (create, read, update, delete).

Usage:
    python sg_crud_test.py --region us-west-2 --cidr 10.95.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "sg_crud",
    "tests": {
        "create_vpc": {"passed": true, "vpc_id": "vpc-xxx"},
        "create_sg": {"passed": true, "sg_id": "sg-xxx"},
        "read_sg": {"passed": true, "name": "...", "description": "..."},
        "update_sg_add_rule": {"passed": true},
        "update_sg_modify_rule": {"passed": true},
        "update_sg_remove_rule": {"passed": true},
        "delete_sg": {"passed": true},
        "verify_deleted": {"passed": true}
    }
}
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import delete_with_retry, handle_aws_errors
from common.vpc import create_test_vpc

logger = logging.getLogger(__name__)


def test_create_sg(ec2: Any, vpc_id: str, sg_name: str) -> dict[str, Any]:
    """Test creating a security group."""
    result: dict[str, Any] = {"passed": False}
    try:
        sg = ec2.create_security_group(
            GroupName=sg_name,
            Description="ISV test SG for CRUD lifecycle",
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
        result["sg_id"] = sg_id

        # Remove the default egress rule for a clean slate
        ec2.revoke_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        )

        result["passed"] = True
        result["message"] = f"Created security group {sg_id}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_read_sg(ec2: Any, sg_id: str, expected_name: str) -> dict[str, Any]:
    """Test reading / describing a security group."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        sg_info = response["SecurityGroups"][0]

        result["name"] = sg_info["GroupName"]
        result["description"] = sg_info["Description"]
        result["vpc_id"] = sg_info["VpcId"]
        result["inbound_rule_count"] = len(sg_info.get("IpPermissions", []))
        result["outbound_rule_count"] = len(sg_info.get("IpPermissionsEgress", []))

        if sg_info["GroupName"] == expected_name:
            result["passed"] = True
            result["message"] = f"SG {sg_id} readable, name matches"
        else:
            result["error"] = f"Name mismatch: expected {expected_name}, got {sg_info['GroupName']}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_update_sg_add_rule(ec2: Any, sg_id: str) -> dict[str, Any]:
    """Test adding an inbound rule to a security group."""
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8", "Description": "HTTPS from internal"}],
                }
            ],
        )

        # Verify rule was added
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        inbound = response["SecurityGroups"][0].get("IpPermissions", [])

        has_https = any(r.get("FromPort") == 443 and r.get("ToPort") == 443 for r in inbound)

        if has_https:
            result["passed"] = True
            result["rule_added"] = "tcp/443 from 10.0.0.0/8"
            result["message"] = "Inbound HTTPS rule added and verified"
        else:
            result["error"] = "Rule not found after authorize_ingress"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_update_sg_modify_rule(ec2: Any, sg_id: str) -> dict[str, Any]:
    """Test modifying a rule (revoke old, add replacement)."""
    result: dict[str, Any] = {"passed": False}
    try:
        # Revoke the HTTPS rule we added
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                }
            ],
        )

        # Add a replacement rule on a different port
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 8443,
                    "ToPort": 8443,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8", "Description": "Alt HTTPS from internal"}],
                }
            ],
        )

        # Verify: old rule gone, new rule present
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        inbound = response["SecurityGroups"][0].get("IpPermissions", [])

        has_443 = any(r.get("FromPort") == 443 for r in inbound)
        has_8443 = any(r.get("FromPort") == 8443 and r.get("ToPort") == 8443 for r in inbound)

        if has_8443 and not has_443:
            result["passed"] = True
            result["rule_before"] = "tcp/443"
            result["rule_after"] = "tcp/8443"
            result["message"] = "Rule modified: 443 -> 8443"
        else:
            result["error"] = f"Unexpected state: has_443={has_443}, has_8443={has_8443}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_update_sg_remove_rule(ec2: Any, sg_id: str) -> dict[str, Any]:
    """Test removing a rule and verifying the SG is clean."""
    result: dict[str, Any] = {"passed": False}
    try:
        # Remove the 8443 rule
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 8443,
                    "ToPort": 8443,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                }
            ],
        )

        # Verify no inbound rules remain
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        inbound = response["SecurityGroups"][0].get("IpPermissions", [])

        if not inbound:
            result["passed"] = True
            result["message"] = "All inbound rules removed, SG is clean"
        else:
            result["error"] = f"Unexpected inbound rules remain: {inbound}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_delete_sg(ec2: Any, sg_id: str) -> dict[str, Any]:
    """Test deleting a security group."""
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.delete_security_group(GroupId=sg_id)
        result["passed"] = True
        result["message"] = f"Security group {sg_id} deleted"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_verify_deleted(ec2: Any, sg_id: str) -> dict[str, Any]:
    """Verify a deleted security group no longer exists."""
    result: dict[str, Any] = {"passed": False}

    time.sleep(2)

    try:
        ec2.describe_security_groups(GroupIds=[sg_id])
        result["error"] = f"Security group {sg_id} still exists after deletion"
    except ClientError as e:
        if "InvalidGroup.NotFound" in str(e):
            result["passed"] = True
            result["message"] = f"Security group {sg_id} confirmed deleted"
        else:
            result["error"] = str(e)
    return result


@handle_aws_errors
def main() -> int:
    """Run Security Group CRUD lifecycle tests against AWS EC2."""
    parser = argparse.ArgumentParser(description="Test Security Group CRUD operations")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.95.0.0/16", help="CIDR for test VPC")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    suffix = str(uuid.uuid4())[:8]
    sg_name = f"isv-sg-crud-test-{suffix}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sg_crud",
        "status": "failed",
        "tests": {},
    }

    vpc_id = None
    sg_id = None

    try:
        # Setup: create a VPC to hold the SG
        vpc_result = create_test_vpc(ec2, args.cidr, f"isv-sg-crud-vpc-{suffix}")
        result["tests"]["create_vpc"] = vpc_result

        if not vpc_result["passed"]:
            print(json.dumps(result, indent=2))
            return 1

        vpc_id = vpc_result["vpc_id"]
        result["network_id"] = vpc_id

        # Test 1: Create SG
        create_result = test_create_sg(ec2, vpc_id, sg_name)
        result["tests"]["create_sg"] = create_result

        if not create_result["passed"]:
            print(json.dumps(result, indent=2))
            return 1

        sg_id = create_result["sg_id"]

        # Test 2: Read SG
        result["tests"]["read_sg"] = test_read_sg(ec2, sg_id, sg_name)

        # Test 3: Update - add rule
        result["tests"]["update_sg_add_rule"] = test_update_sg_add_rule(ec2, sg_id)

        # Test 4: Update - modify rule
        result["tests"]["update_sg_modify_rule"] = test_update_sg_modify_rule(ec2, sg_id)

        # Test 5: Update - remove rule
        result["tests"]["update_sg_remove_rule"] = test_update_sg_remove_rule(ec2, sg_id)

        # Test 6: Delete SG
        delete_result = test_delete_sg(ec2, sg_id)
        result["tests"]["delete_sg"] = delete_result

        if delete_result["passed"]:
            # Test 7: Verify deleted
            result["tests"]["verify_deleted"] = test_verify_deleted(ec2, sg_id)
            sg_id = None  # Mark as deleted

        # Overall success
        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        # Cleanup: delete SG if still exists, then VPC. Route through
        # delete_with_retry so a transient error (throttling, connection
        # reset) does not orphan the resource on a single-shot delete.
        # Capture the return value so a failed SG cleanup doesn't make us
        # attempt the VPC delete (which will fail on SG dependency) and
        # doesn't silently coexist with result["success"] = True.
        sg_deleted = True
        if sg_id:
            sg_deleted = delete_with_retry(
                ec2.delete_security_group,
                GroupId=sg_id,
                resource_desc=f"security group {sg_id}",
            )
            if not sg_deleted:
                result.setdefault("cleanup_errors", []).append(f"security group {sg_id}")
        if vpc_id:
            if not sg_deleted:
                result.setdefault("cleanup_errors", []).append(f"skipped VPC {vpc_id} delete because SG cleanup failed")
            elif not delete_with_retry(
                ec2.delete_vpc,
                VpcId=vpc_id,
                resource_desc=f"VPC {vpc_id}",
            ):
                result.setdefault("cleanup_errors", []).append(f"VPC {vpc_id}")
        if result.get("cleanup_errors"):
            result["success"] = False
            result["status"] = "failed"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
