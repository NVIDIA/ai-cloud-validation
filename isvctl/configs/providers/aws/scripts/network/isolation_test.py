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

"""Test VPC isolation - verify no connectivity between separate VPCs.

Usage:
    python isolation_test.py --region us-west-2 --cidr-a 10.97.0.0/16 --cidr-b 10.96.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_vpc_a": {"passed": true, "vpc_id": "vpc-xxx"},
        "create_vpc_b": {"passed": true, "vpc_id": "vpc-yyy"},
        "no_peering": {"passed": true},
        "no_cross_routes_a": {"passed": true},
        "no_cross_routes_b": {"passed": true},
        "sg_isolation_a": {"passed": true},
        "sg_isolation_b": {"passed": true}
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
from common.vpc import create_test_vpc, delete_vpc


def test_no_peering(ec2: Any, vpc_a: str, vpc_b: str) -> dict[str, Any]:
    """Verify no VPC peering exists between VPCs."""
    result = {"passed": False}

    try:
        # Check for peering connections involving either VPC
        response = ec2.describe_vpc_peering_connections(
            Filters=[
                {
                    "Name": "status-code",
                    "Values": ["active", "pending-acceptance", "provisioning"],
                }
            ]
        )

        # Filter for connections involving our VPCs
        relevant_peerings = []
        for pc in response["VpcPeeringConnections"]:
            requester = pc.get("RequesterVpcInfo", {}).get("VpcId")
            accepter = pc.get("AccepterVpcInfo", {}).get("VpcId")
            if requester in [vpc_a, vpc_b] or accepter in [vpc_a, vpc_b]:
                relevant_peerings.append(pc["VpcPeeringConnectionId"])

        if not relevant_peerings:
            result["passed"] = True
            result["message"] = "No VPC peering connections found between VPCs"
        else:
            result["error"] = f"Found peering connections: {relevant_peerings}"
            result["peering_ids"] = relevant_peerings
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_no_cross_routes(ec2: Any, vpc_id: str, other_cidr: str) -> dict[str, Any]:
    """Verify no routes to the other VPC's CIDR exist."""
    result = {"passed": False}

    try:
        response = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])

        cross_routes = []
        for rt in response["RouteTables"]:
            for route in rt.get("Routes", []):
                dest = route.get("DestinationCidrBlock", "")
                # Check if route points to other VPC's CIDR range
                if dest and dest != "local" and _cidrs_overlap(dest, other_cidr):
                    cross_routes.append(
                        {
                            "route_table": rt["RouteTableId"],
                            "destination": dest,
                        }
                    )

        if not cross_routes:
            result["passed"] = True
            result["message"] = f"No routes to {other_cidr} found"
        else:
            result["error"] = "Found cross-VPC routes"
            result["routes"] = cross_routes
    except ClientError as e:
        result["error"] = str(e)

    return result


def _cidrs_overlap(cidr1: str, cidr2: str) -> bool:
    """Check if two CIDRs overlap (simplified check)."""
    # Simple prefix check - in reality you'd want proper CIDR math
    prefix1 = cidr1.split("/")[0].rsplit(".", 2)[0]  # Get first two octets
    prefix2 = cidr2.split("/")[0].rsplit(".", 2)[0]
    return prefix1 == prefix2


def test_sg_isolation(ec2: Any, vpc_id: str, other_cidr: str) -> dict[str, Any]:
    """Verify default security group doesn't allow traffic from other VPC."""
    result = {"passed": False}

    try:
        response = ec2.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": ["default"]},
            ]
        )

        if not response["SecurityGroups"]:
            result["error"] = "No default security group found"
            return result

        sg = response["SecurityGroups"][0]
        allows_other_vpc = False

        for rule in sg.get("IpPermissions", []):
            for ip_range in rule.get("IpRanges", []):
                cidr = ip_range.get("CidrIp", "")
                if cidr == "0.0.0.0/0" or _cidrs_overlap(cidr, other_cidr):
                    allows_other_vpc = True
                    break

        if not allows_other_vpc:
            result["passed"] = True
            result["message"] = "Default SG doesn't allow traffic from other VPC"
        else:
            result["error"] = "Default SG allows traffic from other VPC CIDR"
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test VPC isolation")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr-a", default="10.97.0.0/16", help="CIDR for VPC A")
    parser.add_argument("--cidr-b", default="10.96.0.0/16", help="CIDR for VPC B")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    suffix = str(uuid.uuid4())[:8]

    result = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    vpc_a_id = None
    vpc_b_id = None

    try:
        # Test 1: Create VPC A
        vpc_a_result = create_test_vpc(ec2, args.cidr_a, f"isv-isolation-a-{suffix}")
        result["tests"]["create_vpc_a"] = vpc_a_result

        if not vpc_a_result["passed"]:
            print(json.dumps(result, indent=2))
            return 1

        vpc_a_id = vpc_a_result["vpc_id"]

        # Test 2: Create VPC B
        vpc_b_result = create_test_vpc(ec2, args.cidr_b, f"isv-isolation-b-{suffix}")
        result["tests"]["create_vpc_b"] = vpc_b_result

        if not vpc_b_result["passed"]:
            print(json.dumps(result, indent=2))
            return 1

        vpc_b_id = vpc_b_result["vpc_id"]

        result["vpc_a"] = {"id": vpc_a_id, "cidr": args.cidr_a}
        result["vpc_b"] = {"id": vpc_b_id, "cidr": args.cidr_b}

        # Test 3: No peering between VPCs
        peering_result = test_no_peering(ec2, vpc_a_id, vpc_b_id)
        result["tests"]["no_peering"] = peering_result

        # Test 4: No cross-routes from VPC A to VPC B
        routes_a_result = test_no_cross_routes(ec2, vpc_a_id, args.cidr_b)
        result["tests"]["no_cross_routes_a"] = routes_a_result

        # Test 5: No cross-routes from VPC B to VPC A
        routes_b_result = test_no_cross_routes(ec2, vpc_b_id, args.cidr_a)
        result["tests"]["no_cross_routes_b"] = routes_b_result

        # Test 6: SG isolation for VPC A
        sg_a_result = test_sg_isolation(ec2, vpc_a_id, args.cidr_b)
        result["tests"]["sg_isolation_a"] = sg_a_result

        # Test 7: SG isolation for VPC B
        sg_b_result = test_sg_isolation(ec2, vpc_b_id, args.cidr_a)
        result["tests"]["sg_isolation_b"] = sg_b_result

        # Check overall success
        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        # Cleanup
        if vpc_a_id:
            delete_vpc(ec2, vpc_a_id)
        if vpc_b_id:
            delete_vpc(ec2, vpc_b_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
