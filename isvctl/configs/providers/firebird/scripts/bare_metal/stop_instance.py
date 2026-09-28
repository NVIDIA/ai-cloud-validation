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

"""Power off a Firebird bare-metal server and verify it reaches the stopped state.

The server is powered off through the platform API (not deprovisioned): it keeps
its OS, identity, and IP address.

Usage:
    python stop_instance.py --instance-id bm.xxx

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "state": "stopped",
    "stop_initiated": true
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


def main() -> int:
    """Power off the BM and wait for it to reach STOPPED.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Power off Firebird BM")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--timeout", type=int, default=1800, help="Overall timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "stop_initiated": False,
    }

    deadline = time.monotonic() + args.timeout
    try:
        client = FirebirdClient()
        bm = client.get_bm(args.instance_id)

        if bm.get("state") == "STOPPED":
            # Already stopped - idempotent no-op
            result["state"] = to_state(bm)
            result["stop_initiated"] = True
            result["success"] = True
            print(f"  BM {args.instance_id} already stopped (no-op)", file=sys.stderr)
            print(json.dumps(result, indent=2))
            return 0

        if bm.get("state") != "RUNNING":
            result["state"] = to_state(bm)
            result["error"] = f"BM is {result['state']}, expected running"
            print(json.dumps(result, indent=2))
            return 1

        print(f"Powering off BM {args.instance_id}...", file=sys.stderr)
        client.bm_action(args.instance_id, "power-off", timeout=remaining(deadline))
        result["stop_initiated"] = True

        bm = client.wait_bm(args.instance_id, ("STOPPED",), remaining(deadline), power="OFF")
        result["instance_id"] = bm.get("id")
        result["state"] = to_state(bm)
        result["success"] = True
        print("Stop completed successfully!", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
