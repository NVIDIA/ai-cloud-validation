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

"""Reinstall a bare-metal node from its stock OS image - TEMPLATE.

This script is called during the "test" phase. It must:
  1. Reinstall / reimage the node from a stock OS image (preserving the
     instance identity but wiping the OS / root disk)
  2. Confirm the node reaches "running" state
  3. Verify SSH connectivity is restored
  4. Print a JSON object to stdout

Reinstall vs power-cycle vs reboot:
  - Reboot is an OS-level restart; hardware and disk state are preserved.
  - Power-cycle is a full hardware reset; disk state is preserved.
  - Reinstall wipes the root disk and re-provisions the OS from a stock image.
    The instance identity (instance ID, MAC, etc.) is preserved but everything
    on the root filesystem is replaced.

Skipped by default in `tests/bare_metal.yaml` because reinstalling a bare-metal
node can be very slow on some platforms (AWS needs ~30-45 min for a root
volume swap). Set `skip: false` on the step once this stub is implemented.

Required JSON output fields (the downstream validations expect these):
  {
    "success": true,                # boolean - did the reinstall succeed?
    "platform": "bm",               # string  - always "bm"
    "instance_id": "...",           # string  - instance identifier (unchanged)
    "state": "running",             # string  - must be "running" after reinstall
    "public_ip": "54.x.x.x",        # string  - public IP (may change after reinstall)
    "key_file": "/tmp/key.pem",     # string  - path to SSH private key
    "ssh_user": "ubuntu",           # string  - SSH username for the stock image
    "ssh_ready": true,              # boolean - can we SSH after reinstall?
    "reinstall_method": "...",      # string  - e.g. "root_volume_swap", "reimage_api"
    "reinstall_seconds": 1800       # int     - seconds from initiation to SSH ready
  }

On failure, set "success": false and include an "error" field.

Usage:
    python reinstall_instance.py --instance-id <id> --region <region> \
        --key-file /tmp/key.pem --public-ip 54.x.x.x

Reference implementation: ../../aws/bare_metal/reinstall_instance.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Reinstall bare-metal node from stock OS (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Reinstall bare-metal node from stock OS (template)")
    parser.add_argument("--instance-id", required=True, help="Instance identifier")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--public-ip", required=True, help="Instance public IP")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username for the stock image")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "state": "",
        "public_ip": args.public_ip,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "ssh_ready": False,
        "reinstall_method": "",
        "reinstall_seconds": None,
    }

    try:
        # ╔══════════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's reinstall logic   ║
        # ║                                                                  ║
        # ║  Example (pseudocode):                                           ║
        # ║    client = MyCloudClient(region=args.region)                    ║
        # ║                                                                  ║
        # ║    1. Verify instance is running before reinstall:               ║
        # ║       info = client.describe_instance(args.instance_id)          ║
        # ║       assert info.state == "running"                             ║
        # ║                                                                  ║
        # ║    2. Trigger reinstall (pick whichever your platform supports): ║
        # ║       start_time = time.time()                                   ║
        # ║       client.reimage_instance(args.instance_id)                  ║
        # ║       #   -- OR --                                               ║
        # ║       #   swap the root volume from a stock-image snapshot:      ║
        # ║       #   stop -> detach root -> create new root from snapshot   ║
        # ║       #   -> attach new root -> start                            ║
        # ║       result["reinstall_method"] = "reimage_api"                 ║
        # ║                                                                  ║
        # ║    3. Wait for running state:                                    ║
        # ║       client.wait_until_running(args.instance_id, timeout=3000)  ║
        # ║       info = client.describe_instance(args.instance_id)          ║
        # ║       result["state"] = info.state                               ║
        # ║       result["public_ip"] = info.public_ip                       ║
        # ║                                                                  ║
        # ║    4. Verify SSH connectivity against the fresh OS:              ║
        # ║       ssh_ok = wait_for_ssh(                                     ║
        # ║           host=result["public_ip"],                              ║
        # ║           user=args.ssh_user,                                    ║
        # ║           key_file=args.key_file,                                ║
        # ║           max_attempts=80,                                       ║
        # ║           interval=15,                                           ║
        # ║       )                                                          ║
        # ║       result["ssh_ready"] = ssh_ok                               ║
        # ║       result["reinstall_seconds"] = int(time.time() - start_time)║
        # ║       result["success"] = ssh_ok                                 ║
        # ╚══════════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            result["state"] = "running"
            result["ssh_ready"] = True
            result["reinstall_method"] = "demo_reimage"
            result["reinstall_seconds"] = 1800
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's reinstall logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
