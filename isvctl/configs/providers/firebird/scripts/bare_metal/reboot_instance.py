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

"""Reboot a Firebird bare-metal server and verify recovery.

Reboot goes through the platform API (BMC-driven restart). The reboot is
affirmed by comparing the host's boot time (now - uptime) with the moment the
reboot was requested.

Usage:
    python reboot_instance.py --instance-id bm.xxx --key-file /tmp/key --public-ip 10.x.x.x

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "state": "running",
    "public_ip": "10.x.x.x",
    "key_file": "/tmp/key",
    "reboot_initiated": true,
    "ssh_ready": true,
    "uptime_seconds": 45.0,
    "reboot_confirmed": true
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, remaining, to_state
from common.ssh_utils import get_uptime, wait_for_ssh


def main() -> int:
    """Reboot the BM and wait for it to come back healthy.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Reboot Firebird BM")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--public-ip", required=True, help="BM IP address")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--timeout", type=int, default=3000, help="Overall timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "reboot_initiated": False,
        "ssh_ready": False,
    }

    deadline = time.monotonic() + args.timeout
    try:
        client = FirebirdClient()
        bm = client.get_bm(args.instance_id)
        if bm.get("state") != "RUNNING":
            result["state"] = to_state(bm)
            result["error"] = f"BM is {result['state']}, expected running"
            print(json.dumps(result, indent=2))
            return 1

        pre_uptime = get_uptime(args.public_ip, args.ssh_user, args.key_file)
        if pre_uptime is not None:
            result["pre_reboot_uptime"] = round(pre_uptime, 1)

        print(f"Rebooting BM {args.instance_id}...", file=sys.stderr)
        reboot_requested_at = time.time()
        client.bm_action(args.instance_id, "reboot", timeout=remaining(deadline))
        result["reboot_initiated"] = True

        bm = client.wait_bm(args.instance_id, ("RUNNING",), remaining(deadline), power="ON", need_ip=True)
        result["instance_id"] = bm.get("id")
        result["state"] = to_state(bm)
        result["public_ip"] = bm["ipAddress"]
        result["private_ip"] = bm["ipAddress"]

        print("Waiting for SSH after reboot...", file=sys.stderr)
        result["ssh_ready"] = wait_for_ssh(bm["ipAddress"], args.ssh_user, args.key_file, deadline)
        if not result["ssh_ready"]:
            raise RuntimeError("SSH not ready after reboot")

        post_uptime = get_uptime(bm["ipAddress"], args.ssh_user, args.key_file)
        if post_uptime is None:
            result["reboot_confirmed"] = False
            raise RuntimeError("Could not sample post-reboot uptime via SSH (cannot affirm reboot)")
        result["uptime_seconds"] = round(post_uptime, 1)

        # The kernel booted after our request => the reboot happened.
        booted_after_request = time.time() - post_uptime >= reboot_requested_at
        uptime_reset = pre_uptime is not None and post_uptime < pre_uptime
        result["reboot_confirmed"] = booted_after_request or uptime_reset
        if not result["reboot_confirmed"]:
            raise RuntimeError(f"Host uptime {post_uptime:.0f}s predates the reboot request")

        result["success"] = True
        print("Reboot completed successfully!", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
