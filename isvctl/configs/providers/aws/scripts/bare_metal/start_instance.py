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

"""Start a stopped AWS bare-metal EC2 instance and verify it returns to running state.

Starts the instance using the EC2 API, waits for it to return to running
state with passing status checks, then verifies SSH connectivity.
Bare-metal instances require longer waiter timeouts than VMs due to
hardware power-on (IPMI, BMC, full POST cycle, OS boot).

Usage:
    python start_instance.py --instance-id i-xxx --region us-west-2 \\
        --key-file /tmp/key.pem

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "state": "running",
    "public_ip": "54.x.x.x",
    "private_ip": "10.0.1.5",
    "key_file": "/tmp/key.pem",
    "ssh_user": "ubuntu",
    "start_initiated": true,
    "ssh_ready": true,
    "region": "us-west-2"
}
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/aws/scripts/ (for common.*)
import boto3
from common.ec2 import wait_for_public_ip
from common.ssh_utils import wait_for_ssh


def main() -> int:
    """Start a stopped bare-metal EC2 instance and wait for it to be healthy.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Start stopped bare-metal EC2 instance")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "region": args.region,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "start_initiated": False,
        "ssh_ready": False,
    }

    try:
        # ============================================================
        # Step 1: Verify instance is currently stopped
        # ============================================================
        print("Verifying instance is stopped before start...", file=sys.stderr)
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        current_state = instance["State"]["Name"]

        if current_state != "stopped":
            result["error"] = f"Instance is {current_state}, expected stopped"
            result["state"] = current_state
            print(json.dumps(result, indent=2))
            return 1

        # ============================================================
        # Step 2: Start the instance via EC2 API
        # ============================================================
        print(f"Starting bare-metal instance {args.instance_id}...", file=sys.stderr)
        ec2.start_instances(InstanceIds=[args.instance_id])
        result["start_initiated"] = True
        print("  Start API call succeeded", file=sys.stderr)

        # ============================================================
        # Step 3: Wait for instance status checks to pass
        # Bare metal needs longer waiter: full POST/BIOS/OS boot cycle
        # ============================================================
        print("Waiting for bare-metal instance status checks (POST/BIOS/OS boot)...", file=sys.stderr)
        waiter = ec2.get_waiter("instance_status_ok")
        waiter.wait(
            InstanceIds=[args.instance_id],
            WaiterConfig={"Delay": 30, "MaxAttempts": 90},  # up to 45 min
        )
        print("  Instance status checks passed", file=sys.stderr)

        # ============================================================
        # Step 4: Get updated instance details
        # ============================================================
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        result["state"] = instance["State"]["Name"]
        result["private_ip"] = instance.get("PrivateIpAddress")

        # Poll for the fresh public IP rather than fall back to the
        # pre-stop value - safe on AWS but silently stale on NCPs that
        # release the ephemeral IP on stop.
        fresh_ip = instance.get("PublicIpAddress") or wait_for_public_ip(ec2, args.instance_id)
        if not fresh_ip:
            result["error"] = "Instance has no public IP after start (timed out polling)"
            print(json.dumps(result, indent=2))
            return 1
        result["public_ip"] = fresh_ip

        # ============================================================
        # Step 5: Wait for SSH to be ready
        # ============================================================
        print("Waiting for SSH to be ready...", file=sys.stderr)
        ssh_ready = wait_for_ssh(result["public_ip"], args.ssh_user, args.key_file)
        result["ssh_ready"] = ssh_ready

        if not ssh_ready:
            result["error"] = "SSH not ready after start"
            print("WARNING: SSH did not become ready after start", file=sys.stderr)
            print(json.dumps(result, indent=2))
            return 1

        result["success"] = True
        print("Start completed successfully!", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
