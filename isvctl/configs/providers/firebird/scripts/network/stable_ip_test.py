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

"""Private IP stability of a provisioned BM across power-off/power-on (SDN11-01).

Uses the same power actions as the bare_metal stop/start steps:
  create_instance  the configured BM is RUNNING (it is reused, not created)
  record_ip        its API-reported ``ipAddress``
  stop_instance    POST .../power-off, Operation awaited, BM reads STOPPED/OFF
  start_instance   POST .../power-on, Operation awaited, BM RUNNING/ON with an IP,
                   and SSH answers again
  ip_unchanged     the IP after power-on equals the IP before
If the step fails after powering the BM off, it powers it back on. Skips when
no provisioned BM is configured.

Usage:
    python stable_ip_test.py --instance-id bm.xxx --key-file /tmp/key [--ssh-user ubuntu]

Output JSON:
{
    "success": true,
    "platform": "network",
    "instance_id": "bm.xxx",
    "tests": {"create_instance": {"passed": true}, "record_ip": {"passed": true, "ip": "172.16.240.10"},
              "stop_instance": {"passed": true}, "start_instance": {"passed": true},
              "ip_unchanged": {"passed": true, "ip_before": "172.16.240.10", "ip_after": "172.16.240.10"}}
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, remaining
from common.probes import PRIMARY_MISSING, running_host, skipped
from common.ssh_utils import wait_for_ssh

KEYS = ("create_instance", "record_ip", "stop_instance", "start_instance", "ip_unchanged")


def power_on(client: FirebirdClient, bm_id: str, deadline: float) -> dict[str, Any]:
    """Power the BM on and wait until it is RUNNING with an IP; return it."""
    client.bm_action(bm_id, "power-on", timeout=remaining(deadline))
    return client.wait_bm(bm_id, ("RUNNING",), remaining(deadline), power="ON", need_ip=True)


def main() -> int:
    """Stop and start the BM and compare its IP before and after.

    Returns:
        0 when the IP is stable, 0 on skip, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird stable private IP test")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID)")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--timeout", type=int, default=4500, help="Overall timeout in seconds")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "network", "instance_id": args.instance_id}
    if not (args.instance_id and args.key_file):
        print(json.dumps(skipped(result, PRIMARY_MISSING), indent=2))
        return 0
    result["tests"] = tests
    deadline = time.monotonic() + args.timeout
    client = None
    powered_off = False
    try:
        client = FirebirdClient()
        host = running_host(client, args.instance_id, args.ssh_user, args.key_file)
        tests["create_instance"] = {"passed": True, "message": f"reused provisioned BM {host.bm_id}"}
        tests["record_ip"] = {"passed": True, "ip": host.ip}

        log(f"Powering off {host.bm_id}...")
        powered_off = True
        client.bm_action(host.bm_id, "power-off", timeout=remaining(deadline))
        client.wait_bm(host.bm_id, ("STOPPED",), remaining(deadline), power="OFF")
        tests["stop_instance"] = {"passed": True}

        log(f"Powering on {host.bm_id}...")
        bm = power_on(client, host.bm_id, deadline)
        powered_off = False
        ssh_ok = wait_for_ssh(bm["ipAddress"], host.user, host.key_file, deadline)
        tests["start_instance"] = {"passed": ssh_ok}
        if not ssh_ok:
            tests["start_instance"]["error"] = "SSH did not answer after power-on"

        ip_after = bm["ipAddress"]
        tests["ip_unchanged"] = {"passed": ip_after == host.ip, "ip_before": host.ip, "ip_after": ip_after}
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        if client and powered_off:
            try:
                power_on(client, args.instance_id, deadline)
            except Exception as e:
                result["cleanup_errors"] = [f"power-on {args.instance_id}: {e}"]

    for key in KEYS:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
