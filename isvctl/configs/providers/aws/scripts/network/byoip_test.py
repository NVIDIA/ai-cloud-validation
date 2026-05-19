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

"""Test Bring-Your-Own-IP (BYOIP) support with non-conflicting custom CIDRs.

Creates VPCs with custom CIDR blocks (including unusual ranges like 7.0.0.0/8)
and verifies they are provisioned correctly without conflicts.

Usage:
    python byoip_test.py --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "custom_cidr_create": {"passed": true, "vpc_id": "vpc-xxx", "cidr": "7.0.0.0/16"},
        "custom_cidr_verify": {"passed": true},
        "standard_cidr_create": {"passed": true, "vpc_id": "vpc-yyy", "cidr": "10.90.0.0/16"},
        "no_conflict": {"passed": true},
        "custom_cidr_subnet": {"passed": true, "subnet_id": "subnet-xxx"}
    }
}
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import delete_with_retry, handle_aws_errors
from common.vpc import create_test_vpc, delete_vpc


def test_custom_cidr_create(ec2: Any, cidr: str, name: str) -> dict[str, Any]:
    """Create a VPC with a custom (BYOIP) CIDR block."""
    return create_test_vpc(ec2, cidr, name)


def test_custom_cidr_verify(ec2: Any, vpc_id: str, expected_cidr: str) -> dict[str, Any]:
    """Verify the VPC was created with the correct custom CIDR."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        if not response["Vpcs"]:
            result["error"] = f"VPC {vpc_id} not found"
            return result

        vpc = response["Vpcs"][0]
        actual_cidr = vpc["CidrBlock"]

        if actual_cidr == expected_cidr:
            result["passed"] = True
            result["cidr"] = actual_cidr
            result["state"] = vpc["State"]
            result["message"] = f"VPC {vpc_id} has correct CIDR {actual_cidr}"
        else:
            result["error"] = f"Expected CIDR {expected_cidr}, got {actual_cidr}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_no_conflict(ec2: Any, vpc_a_id: str, vpc_b_id: str) -> dict[str, Any]:
    """Verify two VPCs with different CIDRs don't conflict."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = ec2.describe_vpcs(VpcIds=[vpc_a_id, vpc_b_id])
        vpcs = response["Vpcs"]

        if len(vpcs) != 2:
            result["error"] = f"Expected 2 VPCs, found {len(vpcs)}"
            return result

        cidr_a = vpcs[0]["CidrBlock"]
        cidr_b = vpcs[1]["CidrBlock"]

        if cidr_a != cidr_b:
            result["passed"] = True
            result["cidr_a"] = cidr_a
            result["cidr_b"] = cidr_b
            result["message"] = f"No conflict: {cidr_a} and {cidr_b} are distinct"
        else:
            result["error"] = f"CIDRs conflict: both are {cidr_a}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_custom_cidr_subnet(ec2: Any, vpc_id: str, vpc_cidr: str) -> dict[str, Any]:
    """Create a subnet within the custom CIDR range to prove it's routable."""
    result: dict[str, Any] = {"passed": False}

    try:
        # Derive a /24 subnet from the VPC CIDR
        base = vpc_cidr.split("/")[0]
        octets = base.split(".")
        subnet_cidr = f"{octets[0]}.{octets[1]}.1.0/24"

        azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
        az = azs["AvailabilityZones"][0]["ZoneName"]

        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=az)
        subnet_id = subnet["Subnet"]["SubnetId"]

        ec2.create_tags(
            Resources=[subnet_id],
            Tags=[
                {"Key": "Name", "Value": "isv-byoip-subnet"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        result["passed"] = True
        result["subnet_id"] = subnet_id
        result["subnet_cidr"] = subnet_cidr
        result["message"] = f"Created subnet {subnet_id} with CIDR {subnet_cidr}"
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test BYOIP support")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--custom-cidr",
        default="100.64.0.0/16",
        help="Custom CIDR to test (e.g. 100.64.0.0/16)",
    )
    parser.add_argument(
        "--standard-cidr",
        default="10.90.0.0/16",
        help="Standard CIDR for conflict check",
    )
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    suffix = str(uuid.uuid4())[:8]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    custom_vpc_id = None
    standard_vpc_id = None

    try:
        # Test 1: Create VPC with custom CIDR
        create_result = test_custom_cidr_create(ec2, args.custom_cidr, f"isv-byoip-custom-{suffix}")
        result["tests"]["custom_cidr_create"] = create_result

        if not create_result["passed"]:
            print(json.dumps(result, indent=2))
            return 1

        custom_vpc_id = create_result["vpc_id"]

        # Test 2: Verify custom CIDR is set correctly
        verify_result = test_custom_cidr_verify(ec2, custom_vpc_id, args.custom_cidr)
        result["tests"]["custom_cidr_verify"] = verify_result

        # Test 3: Create a standard VPC alongside
        standard_result = test_custom_cidr_create(ec2, args.standard_cidr, f"isv-byoip-standard-{suffix}")
        result["tests"]["standard_cidr_create"] = standard_result

        if standard_result.get("passed"):
            standard_vpc_id = standard_result["vpc_id"]

            # Test 4: Verify no conflict
            conflict_result = test_no_conflict(ec2, custom_vpc_id, standard_vpc_id)
            result["tests"]["no_conflict"] = conflict_result

        # Test 5: Create subnet in custom CIDR range
        subnet_result = test_custom_cidr_subnet(ec2, custom_vpc_id, args.custom_cidr)
        result["tests"]["custom_cidr_subnet"] = subnet_result

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        if custom_vpc_id:
            # Clean up subnet first so the VPC delete isn't blocked
            sn = result["tests"].get("custom_cidr_subnet", {})
            if sn.get("subnet_id"):
                delete_with_retry(
                    ec2.delete_subnet,
                    SubnetId=sn["subnet_id"],
                    resource_desc=f"subnet {sn['subnet_id']}",
                )
            delete_vpc(ec2, custom_vpc_id)
        if standard_vpc_id:
            delete_vpc(ec2, standard_vpc_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
