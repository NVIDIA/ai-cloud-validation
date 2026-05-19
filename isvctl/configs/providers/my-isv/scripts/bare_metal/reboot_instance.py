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

"""Reboot bare-metal instance and verify recovery - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It must:
  1. Verify the instance is running before reboot
  2. Issue a reboot command via your platform's API
  3. Wait for the instance to come back (bare-metal takes longer than VM:
     hardware POST, BIOS initialization, OS boot without hypervisor)
  4. Verify SSH connectivity is restored
  5. Check system uptime to confirm the reboot actually occurred
  6. Print a JSON object to stdout

Required JSON output fields (read by InstanceRebootCheck + InstanceStateCheck):
  {
    "success": true,            # boolean - did the reboot + recovery succeed?
    "platform": "bm",           # string  - always "bm"
    "instance_id": "...",       # string  - instance identifier
    "state": "running",         # string  - must be "running" after reboot
    "public_ip": "54.x.x.x",    # string  - public IP (may change after reboot)
    "key_file": "/tmp/key.pem", # string  - path to SSH private key
    "reboot_initiated": true,   # boolean - was the reboot API call made?
    "ssh_ready": true,          # boolean - can we SSH after reboot?
    "uptime_seconds": 45,       # int     - system uptime after reboot (should be low)
    "reboot_confirmed": true    # boolean - optional; InstanceRebootCheck fails if
                                #           present and False (absent == "trust uptime")
  }

On failure, set "success": false and include an "error" field.

Usage:
    python reboot_instance.py --instance-id <id> --region <region> \
        --key-file /tmp/key.pem --public-ip 54.x.x.x

Reference implementation: ../../aws/bare_metal/reboot_instance.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Reboot bare-metal instance (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Reboot bare-metal instance (template)")
    parser.add_argument("--instance-id", required=True, help="Instance identifier")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--public-ip", required=True, help="Instance public IP")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "state": "",
        "public_ip": args.public_ip,
        "key_file": args.key_file,
        "reboot_initiated": False,
        "ssh_ready": False,
        "uptime_seconds": None,
        "reboot_confirmed": None,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's reboot logic      ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║                                                                  ║
    # ║    1. Verify instance is running:                                ║
    # ║       info = client.describe_instance(args.instance_id)          ║
    # ║       assert info.state == "running"                             ║
    # ║                                                                  ║
    # ║    2. Issue reboot:                                              ║
    # ║       client.reboot_instance(args.instance_id)                   ║
    # ║       result["reboot_initiated"] = True                          ║
    # ║                                                                  ║
    # ║    3. Wait for running (BM needs longer: hardware POST/BIOS):    ║
    # ║       time.sleep(120)  # initial wait for reboot to start        ║
    # ║       client.wait_until_running(args.instance_id, timeout=900)   ║
    # ║       info = client.describe_instance(args.instance_id)          ║
    # ║       result["state"] = info.state                               ║
    # ║       result["public_ip"] = info.public_ip                       ║
    # ║                                                                  ║
    # ║    4. Verify SSH connectivity:                                   ║
    # ║       ssh_ok = wait_for_ssh(                                     ║
    # ║           host=result["public_ip"],                              ║
    # ║           key_file=args.key_file,                                ║
    # ║           max_attempts=60,                                       ║
    # ║           interval=15,                                           ║
    # ║       )                                                          ║
    # ║       result["ssh_ready"] = ssh_ok                               ║
    # ║                                                                  ║
    # ║    5. Check uptime (confirms reboot occurred):                   ║
    # ║       uptime = get_uptime_via_ssh(                               ║
    # ║           result["public_ip"], args.key_file                     ║
    # ║       )                                                          ║
    # ║       result["uptime_seconds"] = int(uptime)                     ║
    # ║       result["success"] = True                                   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["state"] = "running"
        result["reboot_initiated"] = True
        result["ssh_ready"] = True
        result["uptime_seconds"] = 45
        result["reboot_confirmed"] = True
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's instance reboot logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
