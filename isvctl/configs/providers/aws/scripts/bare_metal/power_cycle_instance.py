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

"""Power-cycle an AWS bare-metal EC2 instance (hard stop + start).

Unlike reboot (OS-level restart), this performs a full hardware power-cycle:
force-stop the instance, wait for it to reach "stopped", then start it and
wait for recovery. This exercises firmware initialization, BIOS POST, and
a cold OS boot - validating that the node recovers from complete power loss.

Usage:
    python power_cycle_instance.py --instance-id i-xxx --region us-west-2 \\
        --key-file /tmp/key.pem

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "state": "running",
    "public_ip": "54.x.x.x",
    "key_file": "/tmp/key.pem",
    "power_cycle_initiated": true,
    "power_was_off": true,
    "ssh_ready": true,
    "recovery_seconds": 180,
    "region": "us-west-2"
}
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/aws/scripts/ (for common.*)
import boto3
from common.ec2 import wait_for_public_ip
from common.ssh_utils import wait_for_ssh


def main() -> int:
    """Power-cycle a bare-metal EC2 instance: force-stop then start.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Power-cycle bare-metal EC2 instance")
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
        "state": "",
        "region": args.region,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "power_cycle_initiated": False,
        "power_was_off": False,
        "ssh_ready": False,
        "recovery_seconds": None,
    }

    try:
        # ============================================================
        # Step 1: Verify instance is currently running
        # ============================================================
        print("Verifying instance is running before power-cycle...", file=sys.stderr)
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        current_state = instance["State"]["Name"]

        if current_state != "running":
            result["error"] = f"Instance is {current_state}, expected running"
            result["state"] = current_state
            print(json.dumps(result, indent=2))
            return 1

        # ============================================================
        # Step 2: Force-stop the instance (hard power off)
        # Force=True sends an immediate power-off signal, bypassing
        # the OS shutdown sequence - equivalent to pulling the power.
        # ============================================================
        print(f"Force-stopping bare-metal instance {args.instance_id}...", file=sys.stderr)
        ec2.stop_instances(InstanceIds=[args.instance_id], Force=True)
        result["power_cycle_initiated"] = True
        print("  Force-stop API call succeeded", file=sys.stderr)

        # ============================================================
        # Step 3: Wait for the instance to reach stopped state
        # Bare metal needs longer waiter: full hardware power-down
        # ============================================================
        print("Waiting for bare-metal instance to stop (hardware power-down)...", file=sys.stderr)
        waiter = ec2.get_waiter("instance_stopped")
        waiter.wait(
            InstanceIds=[args.instance_id],
            WaiterConfig={"Delay": 30, "MaxAttempts": 60},  # up to 30 min
        )
        result["power_was_off"] = True
        print("  Instance stopped (powered off)", file=sys.stderr)

        # ============================================================
        # Step 4: Start the instance (cold boot)
        # ============================================================
        print(f"Starting bare-metal instance {args.instance_id} (cold boot)...", file=sys.stderr)
        start_time = time.time()
        ec2.start_instances(InstanceIds=[args.instance_id])
        print("  Start API call succeeded", file=sys.stderr)

        # ============================================================
        # Step 5: Wait for instance status checks to pass
        # Full POST/BIOS/OS boot cycle - longer than a reboot
        # ============================================================
        print("Waiting for bare-metal instance status checks (POST/BIOS/OS boot)...", file=sys.stderr)
        waiter = ec2.get_waiter("instance_status_ok")
        waiter.wait(
            InstanceIds=[args.instance_id],
            WaiterConfig={"Delay": 30, "MaxAttempts": 90},  # up to 45 min
        )
        print("  Instance status checks passed", file=sys.stderr)

        # ============================================================
        # Step 6: Get updated instance details
        # ============================================================
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        result["state"] = instance["State"]["Name"]
        result["private_ip"] = instance.get("PrivateIpAddress")

        # Poll for the fresh public IP post-powercycle. Pre-stop IP
        # fallback is safe on AWS, stale on NCPs that release on stop.
        fresh_ip = instance.get("PublicIpAddress") or wait_for_public_ip(ec2, args.instance_id)
        if not fresh_ip:
            result.pop("public_ip", None)
            result["error"] = "Instance has no public IP after power-cycle (timed out polling)"
            print(json.dumps(result, indent=2))
            return 1
        result["public_ip"] = fresh_ip

        # ============================================================
        # Step 7: Wait for SSH to be ready
        # ============================================================
        print("Waiting for SSH to be ready...", file=sys.stderr)
        ssh_ready = wait_for_ssh(result["public_ip"], args.ssh_user, args.key_file)
        result["ssh_ready"] = ssh_ready
        result["recovery_seconds"] = int(time.time() - start_time)

        if not ssh_ready:
            result["error"] = "SSH not ready after power-cycle"
            print("WARNING: SSH did not become ready after power-cycle", file=sys.stderr)
            print(json.dumps(result, indent=2))
            return 1

        result["success"] = True
        print(
            f"Power-cycle completed successfully! (recovery={result['recovery_seconds']}s)",
            file=sys.stderr,
        )

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
