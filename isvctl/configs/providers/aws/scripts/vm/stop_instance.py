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

"""Stop an AWS EC2 instance and verify it reaches the stopped state.

Stops the instance using the EC2 API and waits for it to reach the
"stopped" state, confirming the instance is not destroyed.

Usage:
    python stop_instance.py --instance-id i-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "state": "stopped",
    "stop_initiated": true,
    "region": "us-west-2"
}
"""

import argparse
import json
import os
import sys
from typing import Any

import boto3


def main() -> int:
    """Stop an EC2 instance and wait for it to reach stopped state.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Stop EC2 instance")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "region": args.region,
        "stop_initiated": False,
    }

    try:
        # ============================================================
        # Step 1: Verify instance state
        # ============================================================
        print("Checking instance state before stop...", file=sys.stderr)
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        current_state = instance["State"]["Name"]

        if current_state == "stopped":
            # Already stopped - idempotent no-op
            result["state"] = current_state
            result["stop_initiated"] = True
            result["success"] = True
            print(f"  Instance {args.instance_id} already stopped (no-op)", file=sys.stderr)
            print(json.dumps(result, indent=2))
            return 0

        if current_state != "running":
            result["error"] = f"Instance is {current_state}, expected running"
            result["state"] = current_state
            print(json.dumps(result, indent=2))
            return 1

        # ============================================================
        # Step 2: Stop the instance via EC2 API
        # ============================================================
        print(f"Stopping instance {args.instance_id}...", file=sys.stderr)
        ec2.stop_instances(InstanceIds=[args.instance_id])
        result["stop_initiated"] = True
        print("  Stop API call succeeded", file=sys.stderr)

        # ============================================================
        # Step 3: Wait for the instance to reach stopped state
        # ============================================================
        print("Waiting for instance to stop...", file=sys.stderr)
        waiter = ec2.get_waiter("instance_stopped")
        waiter.wait(
            InstanceIds=[args.instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 40},
        )
        print("  Instance stopped", file=sys.stderr)

        # ============================================================
        # Step 4: Get updated instance state
        # ============================================================
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        result["state"] = instance["State"]["Name"]

        result["success"] = True
        print("Stop completed successfully!", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
