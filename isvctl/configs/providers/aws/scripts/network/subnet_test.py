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

"""Test subnet configuration across availability zones.

Usage:
    python subnet_test.py --region us-west-2 --cidr 10.98.0.0/16 --subnet-count 4

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_vpc": {"passed": true},
        "create_subnets": {"passed": true, "count": 4},
        "az_distribution": {"passed": true, "azs": ["us-west-2a", "us-west-2b"]},
        "subnets_available": {"passed": true},
        "route_table_exists": {"passed": true}
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
from common.errors import handle_aws_errors
from common.vpc import cleanup_vpc_resources, create_test_vpc


def get_availability_zones(ec2: Any, count: int) -> list[str]:
    """Get available AZs in the region."""
    response = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
    azs = [az["ZoneName"] for az in response["AvailabilityZones"]]
    return azs[:count]


def test_create_subnets(ec2: Any, vpc_id: str, cidr_base: str, azs: list[str], count: int) -> dict[str, Any]:
    """Test creating multiple subnets."""
    result = {"passed": False, "subnets": []}

    # Parse base CIDR to create subnet CIDRs
    base_parts = cidr_base.split("/")[0].split(".")
    base_prefix = ".".join(base_parts[:2])

    try:
        for i in range(count):
            az = azs[i % len(azs)]
            subnet_cidr = f"{base_prefix}.{i + 1}.0/24"

            subnet = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=subnet_cidr,
                AvailabilityZone=az,
            )
            subnet_id = subnet["Subnet"]["SubnetId"]

            ec2.create_tags(
                Resources=[subnet_id],
                Tags=[
                    {"Key": "Name", "Value": f"isv-subnet-test-{i}"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            )

            result["subnets"].append(
                {
                    "subnet_id": subnet_id,
                    "cidr": subnet_cidr,
                    "az": az,
                }
            )

        result["count"] = len(result["subnets"])
        result["passed"] = result["count"] == count
        result["message"] = f"Created {result['count']} subnets"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_az_distribution(subnets: list[dict[str, Any]], min_azs: int = 2) -> dict[str, Any]:
    """Test that subnets are distributed across AZs."""
    result = {"passed": False}

    azs = list(set(s["az"] for s in subnets))
    result["azs"] = azs
    result["az_count"] = len(azs)

    if len(azs) >= min_azs:
        result["passed"] = True
        result["message"] = f"Subnets distributed across {len(azs)} AZs"
    else:
        result["error"] = f"Only {len(azs)} AZs used, minimum {min_azs} required"

    return result


def test_subnets_available(ec2: Any, subnet_ids: list[str]) -> dict[str, Any]:
    """Test that all subnets are available."""
    result = {"passed": False}

    try:
        response = ec2.describe_subnets(SubnetIds=subnet_ids)
        states = [(s["SubnetId"], s["State"]) for s in response["Subnets"]]

        all_available = all(state == "available" for _, state in states)
        result["states"] = dict(states)
        result["passed"] = all_available

        if all_available:
            result["message"] = f"All {len(subnet_ids)} subnets are available"
        else:
            result["error"] = "Not all subnets are available"
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_route_table_exists(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test that route tables exist for the VPC."""
    result = {"passed": False}

    try:
        response = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])

        route_tables = response["RouteTables"]
        result["route_table_count"] = len(route_tables)
        result["route_tables"] = [rt["RouteTableId"] for rt in route_tables]

        # At minimum, the main route table should exist
        if len(route_tables) >= 1:
            result["passed"] = True
            result["message"] = f"Found {len(route_tables)} route table(s)"
        else:
            result["error"] = "No route tables found"
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    """Run subnet configuration tests across availability zones.

    Creates a VPC, creates multiple subnets distributed across AZs,
    validates AZ distribution, availability, and route table existence.

    Returns:
        0 on success (all tests pass), 1 on failure
    """
    parser = argparse.ArgumentParser(description="Test subnet configuration")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.98.0.0/16", help="CIDR for test VPC")
    parser.add_argument("--subnet-count", type=int, default=4, help="Number of subnets to create")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    suffix = str(uuid.uuid4())[:8]
    vpc_name = f"isv-subnet-test-{suffix}"

    result = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    vpc_id = None
    subnet_ids = []

    try:
        # Get AZs
        azs = get_availability_zones(ec2, 3)

        # Test 1: Create VPC
        vpc_result = create_test_vpc(ec2, args.cidr, vpc_name)
        result["tests"]["create_vpc"] = vpc_result

        if not vpc_result["passed"]:
            print(json.dumps(result, indent=2))
            return 1

        vpc_id = vpc_result["vpc_id"]
        result["network_id"] = vpc_id

        # Test 2: Create subnets
        subnets_result = test_create_subnets(ec2, vpc_id, args.cidr, azs, args.subnet_count)
        result["tests"]["create_subnets"] = subnets_result
        result["subnets"] = subnets_result.get("subnets", [])
        subnet_ids = [s["subnet_id"] for s in result["subnets"]]

        # Test 3: AZ distribution
        if subnets_result["passed"]:
            az_result = test_az_distribution(result["subnets"])
            result["tests"]["az_distribution"] = az_result

            # Test 4: Subnets available
            available_result = test_subnets_available(ec2, subnet_ids)
            result["tests"]["subnets_available"] = available_result

            # Test 5: Route table exists
            rt_result = test_route_table_exists(ec2, vpc_id)
            result["tests"]["route_table_exists"] = rt_result

        # Check overall success
        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        # Cleanup
        if vpc_id:
            cleanup_vpc_resources(ec2, vpc_id, subnet_ids=subnet_ids)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
