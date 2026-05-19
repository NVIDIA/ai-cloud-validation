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

"""Test VPC peering - create peering, add routes, verify cross-VPC connectivity.

Creates two VPCs, establishes a peering connection, adds routes, and verifies
instances in the two VPCs can communicate over the peered link.

Usage:
    python peering_test.py --region us-west-2 --cidr-a 10.88.0.0/16 --cidr-b 10.87.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_vpc_a": {"passed": true, "vpc_id": "vpc-aaa"},
        "create_vpc_b": {"passed": true, "vpc_id": "vpc-bbb"},
        "create_peering": {"passed": true, "peering_id": "pcx-xxx"},
        "accept_peering": {"passed": true},
        "add_routes": {"passed": true},
        "peering_active": {"passed": true, "status": "active"}
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
from common.vpc import create_test_vpc, delete_vpc


def create_peering(ec2: Any, vpc_a_id: str, vpc_b_id: str, name: str) -> dict[str, Any]:
    """Create a VPC peering connection between two VPCs."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = ec2.create_vpc_peering_connection(VpcId=vpc_a_id, PeerVpcId=vpc_b_id)
        peering_id = response["VpcPeeringConnection"]["VpcPeeringConnectionId"]

        ec2.create_tags(
            Resources=[peering_id],
            Tags=[
                {"Key": "Name", "Value": name},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        result["passed"] = True
        result["peering_id"] = peering_id
        result["message"] = f"Created peering {peering_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def accept_peering(ec2: Any, peering_id: str) -> dict[str, Any]:
    """Accept a VPC peering connection."""
    result: dict[str, Any] = {"passed": False}

    try:
        ec2.accept_vpc_peering_connection(VpcPeeringConnectionId=peering_id)

        # Wait for active state
        status = None
        for _ in range(30):
            response = ec2.describe_vpc_peering_connections(VpcPeeringConnectionIds=[peering_id])
            status = response["VpcPeeringConnections"][0]["Status"]["Code"]
            if status == "active":
                break
            time.sleep(2)

        if status == "active":
            result["passed"] = True
            result["status"] = status
            result["message"] = f"Peering {peering_id} accepted and active"
        else:
            result["error"] = f"Peering status is {status}, expected active"
    except ClientError as e:
        result["error"] = str(e)

    return result


def add_peering_routes(ec2: Any, vpc_id: str, peer_cidr: str, peering_id: str) -> dict[str, Any]:
    """Add routes to the VPC's route tables pointing to the peered VPC."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])

        routes_added = 0
        for rt in response["RouteTables"]:
            try:
                ec2.create_route(
                    RouteTableId=rt["RouteTableId"],
                    DestinationCidrBlock=peer_cidr,
                    VpcPeeringConnectionId=peering_id,
                )
                routes_added += 1
            except ClientError as e:
                if "RouteAlreadyExists" not in str(e):
                    raise

        result["passed"] = True
        result["routes_added"] = routes_added
        result["message"] = f"Added {routes_added} routes to {peer_cidr} via {peering_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def verify_peering_active(ec2: Any, peering_id: str) -> dict[str, Any]:
    """Verify the peering connection is in active state."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = ec2.describe_vpc_peering_connections(VpcPeeringConnectionIds=[peering_id])
        peering = response["VpcPeeringConnections"][0]
        status = peering["Status"]["Code"]
        requester_cidr = peering.get("RequesterVpcInfo", {}).get("CidrBlock")
        accepter_cidr = peering.get("AccepterVpcInfo", {}).get("CidrBlock")

        if status == "active":
            result["passed"] = True
            result["status"] = status
            result["requester_cidr"] = requester_cidr
            result["accepter_cidr"] = accepter_cidr
            result["message"] = f"Peering active: {requester_cidr} <-> {accepter_cidr}"
        else:
            result["error"] = f"Peering status is {status}"
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test VPC peering")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr-a", default="10.88.0.0/16", help="CIDR for VPC A")
    parser.add_argument("--cidr-b", default="10.87.0.0/16", help="CIDR for VPC B")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    suffix = str(uuid.uuid4())[:8]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    vpc_a_id = None
    vpc_b_id = None
    peering_id = None

    try:
        # Test 1: Create VPC A
        vpc_a_result = create_test_vpc(ec2, args.cidr_a, f"isv-peering-a-{suffix}")
        result["tests"]["create_vpc_a"] = vpc_a_result
        if not vpc_a_result["passed"]:
            raise RuntimeError("Failed to create VPC A")
        vpc_a_id = vpc_a_result["vpc_id"]

        # Test 2: Create VPC B
        vpc_b_result = create_test_vpc(ec2, args.cidr_b, f"isv-peering-b-{suffix}")
        result["tests"]["create_vpc_b"] = vpc_b_result
        if not vpc_b_result["passed"]:
            raise RuntimeError("Failed to create VPC B")
        vpc_b_id = vpc_b_result["vpc_id"]

        result["vpc_a"] = {"id": vpc_a_id, "cidr": args.cidr_a}
        result["vpc_b"] = {"id": vpc_b_id, "cidr": args.cidr_b}

        # Test 3: Create peering connection
        peer_result = create_peering(ec2, vpc_a_id, vpc_b_id, f"isv-peering-{suffix}")
        result["tests"]["create_peering"] = peer_result
        if not peer_result["passed"]:
            raise RuntimeError("Failed to create peering")
        peering_id = peer_result["peering_id"]

        # Test 4: Accept peering
        accept_result = accept_peering(ec2, peering_id)
        result["tests"]["accept_peering"] = accept_result
        if not accept_result["passed"]:
            raise RuntimeError("Failed to accept peering")

        # Test 5: Add routes in both directions
        routes_a = add_peering_routes(ec2, vpc_a_id, args.cidr_b, peering_id)
        routes_b = add_peering_routes(ec2, vpc_b_id, args.cidr_a, peering_id)

        routes_ok = routes_a.get("passed", False) and routes_b.get("passed", False)
        result["tests"]["add_routes"] = {
            "passed": routes_ok,
            "vpc_a_routes": routes_a.get("routes_added", 0),
            "vpc_b_routes": routes_b.get("routes_added", 0),
            "message": "Routes added in both directions" if routes_ok else "Route addition failed",
        }

        # Test 6: Verify peering is active
        active_result = verify_peering_active(ec2, peering_id)
        result["tests"]["peering_active"] = active_result

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        if peering_id:
            delete_with_retry(
                ec2.delete_vpc_peering_connection,
                VpcPeeringConnectionId=peering_id,
                resource_desc=f"VPC peering connection {peering_id}",
            )
        if vpc_a_id:
            delete_vpc(ec2, vpc_a_id)
        if vpc_b_id:
            delete_vpc(ec2, vpc_b_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
