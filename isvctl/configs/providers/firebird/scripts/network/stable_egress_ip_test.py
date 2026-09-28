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

"""Egress IP stability of a provisioned BM over repeated probes (DMS05-01).

Over SSH, the BM asks an IP-echo endpoint for its public address --probes times.
  create_instance   the configured BM is RUNNING (it is reused, not created)
  probe_egress_ip   every probe returned an IPv4 address; any failure means the
                    subnet has no internet egress and fails this, not a skip
  egress_ip_stable  all probes returned the same address
Skips when no provisioned BM is configured.

Usage:
    python stable_egress_ip_test.py --instance-id bm.xxx --key-file /tmp/key [--probes 3]

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {"create_instance": {"passed": true},
              "probe_egress_ip": {"passed": true, "probes": 3, "ips": ["203.0.113.7"]},
              "egress_ip_stable": {"passed": true, "egress_ip": "203.0.113.7"}}
}
"""

import argparse
import ipaddress
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log
from common.probes import PRIMARY_MISSING, Host, run, running_host, skipped

KEYS = ("create_instance", "probe_egress_ip", "egress_ip_stable")


def probe(host: Host, endpoint: str) -> str:
    """Return the egress IPv4 address ``endpoint`` reports for ``host``; raise if there is none."""
    code, stdout = run(host, f"curl -fsS --max-time 10 {shlex.quote(endpoint)}")
    if code != 0:
        raise RuntimeError(f"no internet egress from {host.bm_id}: curl exit {code}")
    return str(ipaddress.IPv4Address(stdout.strip()))


def main() -> int:
    """Probe the BM's egress IP repeatedly and emit the DMS05-01 contract.

    Returns:
        0 when the egress IP is stable, 0 on skip, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird stable egress IP test")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID)")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--probes", type=int, default=3, help="Number of probes")
    parser.add_argument("--interval-seconds", type=float, default=2, help="Delay between probes")
    parser.add_argument("--endpoint", default="https://api.ipify.org", help="IP-echo endpoint")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "network", "instance_id": args.instance_id}
    if not (args.instance_id and args.key_file):
        print(json.dumps(skipped(result, PRIMARY_MISSING), indent=2))
        return 0
    result["tests"] = tests
    try:
        host = running_host(FirebirdClient(), args.instance_id, args.ssh_user, args.key_file)
        tests["create_instance"] = {"passed": True, "message": f"reused provisioned BM {host.bm_id}"}
        ips = []
        for index in range(args.probes):
            if index:
                time.sleep(args.interval_seconds)
            ips.append(probe(host, args.endpoint))
        tests["probe_egress_ip"] = {"passed": True, "probes": len(ips), "ips": sorted(set(ips))}
        stable = len(set(ips)) == 1
        tests["egress_ip_stable"] = (
            {"passed": stable, "egress_ip": ips[0]}
            if stable
            else {
                "passed": False,
                "error": f"egress IP changed across probes: {', '.join(sorted(set(ips)))}",
            }
        )
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    for key in KEYS:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
