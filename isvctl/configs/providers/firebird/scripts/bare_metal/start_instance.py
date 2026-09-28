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

"""Power on a stopped Firebird bare-metal server and verify it recovers.

Usage:
    python start_instance.py --instance-id bm.xxx --key-file /tmp/key

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "state": "running",
    "public_ip": "10.x.x.x",
    "key_file": "/tmp/key",
    "ssh_user": "ubuntu",
    "start_initiated": true,
    "ssh_ready": true
}

A power-on Operation that ends FAILED does not fail the step by itself: the
platform still applies the requested power state (a BMC refuses power-on
while the host is still shutting down, and it is applied once the host is off),
so the step waits for the BM readback and reports the failed Operation as
``power_on_operation_error``. It fails when the BM does not reach RUNNING.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdApiError, FirebirdClient, remaining, to_state
from common.ssh_utils import wait_for_ssh


def main() -> int:
    """Power on the BM, wait for RUNNING, and confirm SSH is reachable.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Power on Firebird BM")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--timeout", type=int, default=3000, help="Overall timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "start_initiated": False,
        "ssh_ready": False,
    }

    deadline = time.monotonic() + args.timeout
    try:
        client = FirebirdClient()
        bm = client.get_bm(args.instance_id)

        if bm.get("powerState") == "ON":
            # Already powered on - idempotent no-op, still verify recovery below
            print(f"  BM {args.instance_id} already powered on", file=sys.stderr)
        else:
            print(f"Powering on BM {args.instance_id}...", file=sys.stderr)
            response = client.request("POST", client.bm_path(args.instance_id, "/power-on"), {})
            result["start_initiated"] = True
            try:
                client.wait_operation(response.get("operation") or {}, remaining(deadline))
            except FirebirdApiError as e:
                if e.status:
                    raise  # the operation could not be read: nothing is known
                # The Operation ended FAILED (or never finished), but the platform
                # still applies the requested power state: a power-on sent while
                # the host is still shutting down is refused by the BMC and applied
                # once it is off. Record the failure and judge by the readback.
                result["power_on_operation_error"] = str(e)
                print(f"  power-on operation did not complete ({e}); waiting for the BM itself", file=sys.stderr)
        result["start_initiated"] = True

        try:
            bm = client.wait_bm(args.instance_id, ("RUNNING",), remaining(deadline), power="ON", need_ip=True)
        except Exception as e:
            if "power_on_operation_error" in result:
                raise RuntimeError(
                    f"{result['power_on_operation_error']}; BM did not reach RUNNING afterwards: {e}"
                ) from e
            raise
        result["instance_id"] = bm.get("id")
        result["state"] = to_state(bm)
        result["public_ip"] = bm["ipAddress"]
        result["private_ip"] = bm["ipAddress"]

        print("Waiting for SSH after power-on (POST/BIOS/OS boot)...", file=sys.stderr)
        result["ssh_ready"] = wait_for_ssh(bm["ipAddress"], args.ssh_user, args.key_file, deadline)
        if not result["ssh_ready"]:
            raise RuntimeError("SSH not ready after start")

        result["success"] = True
        print("Start completed successfully!", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
