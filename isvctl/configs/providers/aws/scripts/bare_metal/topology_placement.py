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

"""Validate topology-based placement for a bare-metal EC2 instance.

Verifies that the instance supports placement group membership by:
1. Creating a cluster placement group
2. Verifying the instance's current placement (AZ, tenancy)
3. Describing the placement group to confirm it exists and is available
4. Cleaning up the placement group

Note: AWS bare-metal instances cannot be moved into a placement group
after launch. This test validates that the platform supports placement
groups and that the instance has placement metadata. For a full
topology-aware launch test, the placement group should be specified
at launch time.

Usage:
    python topology_placement.py --instance-id i-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "placement_supported": true,
    "availability_zone": "us-west-2a",
    "placement_group": "isvtest-pg-xxx",
    "placement_strategy": "cluster",
    "operations": {
        "create_group":    {"passed": true},
        "verify_instance": {"passed": true},
        "describe_group":  {"passed": true},
        "delete_group":    {"passed": true}
    }
}
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
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors


def test_create_group(ec2: Any, group_name: str) -> dict[str, Any]:
    """Create a cluster placement group."""
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.create_placement_group(
            GroupName=group_name,
            Strategy="cluster",
            TagSpecifications=[
                {
                    "ResourceType": "placement-group",
                    "Tags": [
                        {"Key": "Name", "Value": group_name},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        result["passed"] = True
        result["message"] = f"Created placement group {group_name}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_verify_instance(ec2: Any, instance_id: str) -> dict[str, Any]:
    """Verify instance placement metadata (AZ, tenancy, group if any)."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {instance_id} not found"
            return result

        instance = reservations[0]["Instances"][0]
        placement = instance.get("Placement", {})

        result["availability_zone"] = placement.get("AvailabilityZone", "")
        result["tenancy"] = placement.get("Tenancy", "")
        result["group_name"] = placement.get("GroupName", "")
        result["instance_type"] = instance.get("InstanceType", "")
        result["passed"] = bool(result["availability_zone"])
        result["message"] = f"Instance {instance_id} in AZ {result['availability_zone']}, tenancy={result['tenancy']}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_describe_group(ec2: Any, group_name: str) -> dict[str, Any]:
    """Describe the placement group and verify it's available."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_placement_groups(GroupNames=[group_name])
        groups = response.get("PlacementGroups", [])
        if not groups:
            result["error"] = f"Placement group {group_name} not found"
            return result

        group = groups[0]
        result["state"] = group.get("State", "")
        result["strategy"] = group.get("Strategy", "")
        result["group_id"] = group.get("GroupId", "")
        result["passed"] = result["state"] == "available"
        result["message"] = f"Group {group_name}: state={result['state']}, strategy={result['strategy']}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_delete_group(ec2: Any, group_name: str) -> dict[str, Any]:
    """Delete the placement group."""
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.delete_placement_group(GroupName=group_name)
        result["passed"] = True
        result["message"] = f"Deleted placement group {group_name}"
    except ClientError as e:
        result["error"] = str(e)
    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Validate topology-based placement (BM)")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    group_name = f"isvtest-pg-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "placement_supported": False,
        "availability_zone": "",
        "placement_group": group_name,
        "placement_strategy": "cluster",
        "operations": {
            "create_group": {"passed": False},
            "verify_instance": {"passed": False},
            "describe_group": {"passed": False},
            "delete_group": {"passed": False},
        },
    }

    try:
        # CREATE placement group
        create_result = test_create_group(ec2, group_name)
        result["operations"]["create_group"] = create_result
        if not create_result["passed"]:
            raise RuntimeError(f"Create group failed: {create_result.get('error')}")

        # VERIFY instance placement metadata
        verify_result = test_verify_instance(ec2, args.instance_id)
        result["operations"]["verify_instance"] = verify_result
        result["availability_zone"] = verify_result.get("availability_zone", "")
        if not verify_result["passed"]:
            raise RuntimeError(f"Verify instance failed: {verify_result.get('error')}")

        # DESCRIBE placement group
        describe_result = test_describe_group(ec2, group_name)
        result["operations"]["describe_group"] = describe_result
        if not describe_result["passed"]:
            raise RuntimeError(f"Describe group failed: {describe_result.get('error')}")

        # DELETE placement group
        delete_result = test_delete_group(ec2, group_name)
        result["operations"]["delete_group"] = delete_result
        if not delete_result["passed"]:
            raise RuntimeError(f"Delete group failed: {delete_result.get('error')}")

        result["placement_supported"] = True
        result["success"] = True

    except RuntimeError as e:
        result["error"] = str(e)
        # Cleanup on partial failure
        try:
            ec2.delete_placement_group(GroupName=group_name)
        except ClientError:
            pass

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
