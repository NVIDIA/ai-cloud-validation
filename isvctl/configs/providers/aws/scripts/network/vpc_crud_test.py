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

"""Test VPC CRUD operations (create, read, update, delete).

Usage:
    python vpc_crud_test.py --region us-west-2 --cidr 10.99.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_vpc": {"passed": true, "vpc_id": "vpc-xxx"},
        "read_vpc": {"passed": true, "state": "available"},
        "update_tags": {"passed": true},
        "update_dns": {"passed": true},
        "delete_vpc": {"passed": true}
    }
}
"""

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import delete_with_retry, handle_aws_errors


def test_create_vpc(ec2: Any, cidr: str, name: str) -> dict[str, Any]:
    """Test VPC creation."""
    result: dict[str, Any] = {"passed": False}
    try:
        vpc = ec2.create_vpc(CidrBlock=cidr)
        vpc_id = vpc["Vpc"]["VpcId"]
        result["vpc_id"] = vpc_id
        result["cidr"] = cidr

        # Tag the VPC
        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {"Key": "Name", "Value": name},
                {"Key": "Environment", "Value": "test"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        result["passed"] = True
        result["message"] = f"Created VPC {vpc_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_read_vpc(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test reading VPC attributes."""
    result = {"passed": False}
    try:
        # Wait for VPC to be available
        waiter = ec2.get_waiter("vpc_available")
        waiter.wait(VpcIds=[vpc_id], WaiterConfig={"Delay": 2, "MaxAttempts": 30})

        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpc = response["Vpcs"][0]

        result["state"] = vpc["State"]
        result["cidr"] = vpc["CidrBlock"]
        result["is_default"] = vpc["IsDefault"]

        # Get DNS attributes
        dns_support = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsSupport")
        dns_hostnames = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsHostnames")

        result["dns_support"] = dns_support["EnableDnsSupport"]["Value"]
        result["dns_hostnames"] = dns_hostnames["EnableDnsHostnames"]["Value"]

        result["passed"] = vpc["State"] == "available"
        result["message"] = f"VPC {vpc_id} is {vpc['State']}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_update_tags(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test updating VPC tags."""
    result = {"passed": False}
    try:
        # Add new tags
        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {"Key": "UpdateTest", "Value": "success"},
                {"Key": "Timestamp", "Value": str(int(time.time()))},
            ],
        )

        # Verify tags were added
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        tags = {t["Key"]: t["Value"] for t in response["Vpcs"][0].get("Tags", [])}

        if "UpdateTest" in tags and tags["UpdateTest"] == "success":
            result["passed"] = True
            result["tags_added"] = ["UpdateTest", "Timestamp"]
            result["message"] = "Tags updated successfully"
        else:
            result["error"] = "Tags not found after update"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_update_dns(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test updating VPC DNS settings."""
    result = {"passed": False}
    try:
        # Enable DNS hostnames
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        # Enable DNS support
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})

        # Verify changes
        dns_hostnames = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsHostnames")
        dns_support = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsSupport")

        hostnames_enabled = dns_hostnames["EnableDnsHostnames"]["Value"]
        support_enabled = dns_support["EnableDnsSupport"]["Value"]

        if hostnames_enabled and support_enabled:
            result["passed"] = True
            result["dns_hostnames"] = hostnames_enabled
            result["dns_support"] = support_enabled
            result["message"] = "DNS settings updated successfully"
        else:
            result["error"] = f"DNS settings not applied: hostnames={hostnames_enabled}, support={support_enabled}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_delete_vpc(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test VPC deletion."""
    result = {"passed": False}
    try:
        ec2.delete_vpc(VpcId=vpc_id)

        # Verify deletion
        time.sleep(2)
        try:
            ec2.describe_vpcs(VpcIds=[vpc_id])
            result["error"] = "VPC still exists after deletion"
        except ClientError as e:
            if "InvalidVpcID.NotFound" in str(e):
                result["passed"] = True
                result["message"] = f"VPC {vpc_id} deleted successfully"
            else:
                result["error"] = str(e)
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test VPC CRUD operations")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.99.0.0/16", help="CIDR block for test VPC")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    suffix = str(uuid.uuid4())[:8]
    vpc_name = f"isv-crud-test-{suffix}"

    result = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
        "vpc_name": vpc_name,
    }

    vpc_id = None

    try:
        # Test 1: Create VPC
        create_result = test_create_vpc(ec2, args.cidr, vpc_name)
        result["tests"]["create_vpc"] = create_result

        if not create_result["passed"]:
            result["error"] = "Failed to create VPC"
            print(json.dumps(result, indent=2))
            return 1

        vpc_id = create_result["vpc_id"]
        result["network_id"] = vpc_id

        # Test 2: Read VPC
        read_result = test_read_vpc(ec2, vpc_id)
        result["tests"]["read_vpc"] = read_result

        # Test 3: Update tags
        tags_result = test_update_tags(ec2, vpc_id)
        result["tests"]["update_tags"] = tags_result

        # Test 4: Update DNS
        dns_result = test_update_dns(ec2, vpc_id)
        result["tests"]["update_dns"] = dns_result

        # Test 5: Delete VPC
        delete_result = test_delete_vpc(ec2, vpc_id)
        result["tests"]["delete_vpc"] = delete_result
        vpc_id = None  # Mark as deleted

        # Check overall success
        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        # Cleanup if VPC still exists - retry on transient errors so a
        # connection reset or throttle doesn't leak the test VPC.
        if vpc_id:
            delete_with_retry(
                ec2.delete_vpc,
                VpcId=vpc_id,
                resource_desc=f"VPC {vpc_id}",
            )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
