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

"""Power on a stopped bare-metal node and verify it returns to running state.

Template stub for ISV NCP Validation. Replace the TODO section with your
platform's API calls to power on a stopped node and verify recovery.

This script must:
  1. Power on the node via your platform's API
  2. Wait for the node to return to "running" state
     Note: BM requires longer timeouts than VMs (full POST/BIOS/OS boot cycle)
  3. Verify SSH connectivity to the node

Required JSON output fields:
  success          (bool) - whether the operation succeeded
  platform         (str)  - always "bm"
  instance_id      (str)  - the started node ID
  state            (str)  - must be "running" after recovery
  public_ip        (str)  - public IP of the node
  key_file         (str)  - path to SSH private key
  start_initiated  (bool) - whether the power-on API call succeeded
  ssh_ready        (bool) - whether SSH is reachable post-start
  error            (str, optional) - human-readable error message when success is false

Usage:
    python start_instance.py --instance-id <id> --region <region> \\
        --key-file /tmp/key.pem --public-ip <ip>

Reference implementation (AWS):
    ../aws/bm/start_instance.py
"""

import argparse
import json
import os
import sys

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Power on stopped bare-metal node and verify recovery and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Power on stopped bare-metal node and verify recovery")
    parser.add_argument("--instance-id", required=True, help="Node ID to power on")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--public-ip", required=True, help="Node public IP address")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "state": "",
        "public_ip": args.public_ip,
        "key_file": args.key_file,
        "start_initiated": False,
        "ssh_ready": False,
    }

    try:
        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's API calls     ║
        # ║                                                              ║
        # ║  1. Power on the node via your platform's API                ║
        # ║     power_on_node(args.instance_id, region=args.region)      ║
        # ║     result["start_initiated"] = True                         ║
        # ║                                                              ║
        # ║  2. Wait for the node to return to "running" state           ║
        # ║     Note: BM needs longer timeouts (POST/BIOS/OS boot)       ║
        # ║     wait_for_running(args.instance_id)                       ║
        # ║                                                              ║
        # ║  3. Verify SSH connectivity                                  ║
        # ║     ssh_ok = wait_for_ssh(                                   ║
        # ║         host=args.public_ip,                                 ║
        # ║         key_file=args.key_file,                              ║
        # ║     )                                                        ║
        # ║     result["ssh_ready"] = ssh_ok                             ║
        # ║                                                              ║
        # ║  4. Populate result                                          ║
        # ║     result["state"] = "running"                              ║
        # ║     result["success"] = True                                 ║
        # ╚══════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            result["instance_id"] = args.instance_id
            result["state"] = "running"
            result["public_ip"] = args.public_ip
            result["key_file"] = args.key_file
            result["start_initiated"] = True
            result["ssh_ready"] = True
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's power-on logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
