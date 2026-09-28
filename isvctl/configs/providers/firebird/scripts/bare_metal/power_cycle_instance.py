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

"""Power-cycle a Firebird bare-metal server (hard power off + power on).

Unlike reboot, this takes the node fully to powered-off (STOPPED / OFF) and
then cold-starts it, exercising firmware init, POST, and a cold OS boot.

Usage:
    python power_cycle_instance.py --instance-id bm.xxx --key-file /tmp/key

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "state": "running",
    "public_ip": "10.x.x.x",
    "key_file": "/tmp/key",
    "power_cycle_initiated": true,
    "power_was_off": true,
    "ssh_ready": true,
    "recovery_seconds": 420
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
from common.ssh_utils import wait_for_ssh


def main() -> int:
    """Power the BM off, confirm it is off, power it on, and time recovery to SSH.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Power-cycle Firebird BM")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--timeout", type=int, default=4700, help="Overall timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "power_cycle_initiated": False,
        "power_was_off": False,
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

        print(f"Powering off BM {args.instance_id}...", file=sys.stderr)
        client.bm_action(args.instance_id, "power-off", timeout=remaining(deadline))
        result["power_cycle_initiated"] = True
        client.wait_bm(args.instance_id, ("STOPPED",), remaining(deadline), power="OFF")
        result["power_was_off"] = True

        print(f"Powering on BM {args.instance_id}...", file=sys.stderr)
        power_on_at = time.monotonic()
        client.bm_action(args.instance_id, "power-on", timeout=remaining(deadline))
        bm = client.wait_bm(args.instance_id, ("RUNNING",), remaining(deadline), power="ON", need_ip=True)
        result["instance_id"] = bm.get("id")
        result["state"] = to_state(bm)
        result["public_ip"] = bm["ipAddress"]
        result["private_ip"] = bm["ipAddress"]

        print("Waiting for SSH after cold start...", file=sys.stderr)
        result["ssh_ready"] = wait_for_ssh(bm["ipAddress"], args.ssh_user, args.key_file, deadline)
        result["recovery_seconds"] = int(time.monotonic() - power_on_at)
        if not result["ssh_ready"]:
            raise RuntimeError("SSH not ready after power-cycle")

        result["success"] = True
        print(f"Power-cycle completed in {result['recovery_seconds']}s", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
