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

"""Reboot a VM instance and validate it comes back healthy.

Template stub for AI Cloud Validation. Replace the TODO section with your
platform's API calls to reboot an instance and verify recovery.

This script must:
  1. Initiate a reboot via your platform's API
  2. Wait for the instance to return to "running" state
  3. Verify SSH connectivity to the instance
  4. Capture system uptime to confirm the reboot occurred

Required JSON output fields (read by InstanceRebootCheck + InstanceStateCheck):
  success           (bool)  - whether the operation succeeded
  platform          (str)   - always "vm"
  instance_id       (str)   - the rebooted instance ID
  state             (str)   - must be "running" after recovery
  public_ip         (str)   - public IP of the instance
  key_file          (str)   - path to SSH private key
  reboot_initiated  (bool)  - whether the reboot API call succeeded
  ssh_ready         (bool)  - whether SSH is reachable post-reboot
  uptime_seconds    (int)   - system uptime after reboot (proves reboot happened)
  reboot_confirmed  (bool, optional) - explicit uptime-based confirmation;
                                       InstanceRebootCheck fails if present
                                       and False (treats absent as "trust uptime")
  error             (str, optional) - human-readable error message provided when success is false

Usage:
    python reboot_instance.py --instance-id i-xxx --region <region> \\
        --key-file /tmp/key.pem --public-ip 54.x.x.x

Reference implementation (AWS):
    ../aws/vm/reboot_instance.py
"""

import argparse
import json
import os
import sys

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Reboot VM instance and verify recovery and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Reboot VM instance and verify recovery")
    parser.add_argument("--instance-id", required=True, help="Instance ID to reboot")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--public-ip", required=True, help="Instance public IP address")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "state": "",
        "public_ip": args.public_ip,
        "key_file": args.key_file,
        "reboot_initiated": False,
        "ssh_ready": False,
        "uptime_seconds": None,
        "reboot_confirmed": None,
    }

    try:
        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's API calls     ║
        # ║                                                              ║
        # ║  1. Reboot the instance via your platform's API              ║
        # ║     reboot_instance(args.instance_id, region=args.region)    ║
        # ║     result["reboot_initiated"] = True                        ║
        # ║                                                              ║
        # ║  2. Wait for the instance to return to "running" state       ║
        # ║     wait_for_running(args.instance_id)                       ║
        # ║     result["state"] = "running"                              ║
        # ║                                                              ║
        # ║  3. Verify SSH connectivity                                  ║
        # ║     ssh_ok = wait_for_ssh(                                   ║
        # ║         host=args.public_ip,                                 ║
        # ║         key_file=args.key_file,                              ║
        # ║     )                                                        ║
        # ║     result["ssh_ready"] = ssh_ok                             ║
        # ║                                                              ║
        # ║  4. Get uptime to confirm reboot (should be low)             ║
        # ║     uptime = ssh_command(args.public_ip, args.key_file,      ║
        # ║         "cat /proc/uptime | cut -d' ' -f1")                  ║
        # ║     result["uptime_seconds"] = int(float(uptime))            ║
        # ║                                                              ║
        # ║  5. Mark success                                             ║
        # ║     result["success"] = True                                 ║
        # ╚══════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            result["state"] = "running"
            result["reboot_initiated"] = True
            result["ssh_ready"] = True
            result["uptime_seconds"] = 45
            result["reboot_confirmed"] = True
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's reboot logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
