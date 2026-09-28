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

"""Time firewall-rule propagation through the Firebird API (SDN02-08).

Network resources are managed through the API. A rule counts as observed when
its Operation completes and its readback is READY; this measures control-plane
completion, not traffic propagation. So:
  add_observed_seconds     POST .../firewall-rules until the Operation completes
                           and the rule reads back READY
  remove_observed_seconds  DELETE until the Operation completes and GET returns 404
Both are polled every --poll-interval seconds. This is control-plane evidence,
not a traffic measurement (that needs two hosts; see sg_scoping_test.py).

The probe rule lives in the run's network (create_network's VPC and its first
subnet, created or supplied); this step creates no network and never deletes
one. A probe rule only reaches READY when at least one prefix is inside an
existing subnet of the VPC; depending on the deployment an invalid rule is
rejected immediately or through its Operation. So the rule denies TCP port 9
from TEST-NET-1 (RFC 5737), any source port, to one ``/32`` inside --subnet-id
(its CIDR is read from the API). No real traffic comes from TEST-NET-1, so the
rule touches none, and it is deleted on every path.

Usage:
    python sg_policy_propagation_test.py --vpc-id vpc.xxx --subnet-id subnet.xxx [--max-propagation-seconds 10]

Output JSON:
{
    "success": true,
    "platform": "network",
    "target_rule_id": "firewall-rule.xxx",
    "add_observed_seconds": 4.1,
    "remove_observed_seconds": 3.2,
    "max_propagation_seconds": 10,
    "tests": {"create_probe_rule": {"passed": true}, "rule_observed": {"passed": true},
              "revoke_probe_rule": {"passed": true}, "removal_observed": {"passed": true},
              "cleanup": {"passed": true}}
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
from common.network import (
    TEST_NET_SRC,
    cleanup_timeout,
    delete_and_wait,
    exists,
    rules_path,
    submit,
    subnet_probe_host,
    tcp_rule,
    unique_name,
)

KEYS = ("create_probe_rule", "rule_observed", "revoke_probe_rule", "removal_observed", "cleanup")


def main() -> int:
    """Create and delete a probe rule, timing each until it is observed.

    Returns:
        0 when every step passed, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird firewall-rule propagation timing")
    parser.add_argument("--vpc-id", required=True, help="VPC to add the probe rule to (vpc.ULID)")
    parser.add_argument("--subnet-id", required=True, help="Subnet of that VPC the rule targets (subnet.ULID)")
    parser.add_argument("--max-propagation-seconds", type=float, default=10, help="Threshold reported to the check")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="Seconds between polls")
    parser.add_argument("--timeout", type=int, default=600, help="Overall timeout in seconds")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "max_propagation_seconds": args.max_propagation_seconds,
        "tests": tests,
    }
    deadline = time.monotonic() + args.timeout
    client = None
    rule_id = ""
    try:
        client = FirebirdClient()
        target = subnet_probe_host(client, args.vpc_id, args.subnet_id)
        body = tcp_rule(unique_name("isv-propagation"), "DENY", TEST_NET_SRC, target, 9)
        started = time.monotonic()
        operation = submit(client, "POST", rules_path(client, args.vpc_id), body)
        rule_id = result["target_rule_id"] = operation["resourceId"]
        tests["create_probe_rule"] = {"passed": True}
        client.wait_operation(operation, remaining(deadline), args.poll_interval)
        while (client.request("GET", rules_path(client, args.vpc_id, rule_id)).get("firewallRule") or {}).get(
            "state"
        ) != "READY":
            if time.monotonic() > deadline:
                raise RuntimeError(f"rule {rule_id} did not read back READY")
            time.sleep(args.poll_interval)
        result["add_observed_seconds"] = round(time.monotonic() - started, 2)
        tests["rule_observed"] = {"passed": True}

        started = time.monotonic()
        delete_and_wait(client, rules_path(client, args.vpc_id, rule_id), remaining(deadline), args.poll_interval)
        tests["revoke_probe_rule"] = {"passed": True}
        while exists(client, rules_path(client, args.vpc_id, rule_id)):
            if time.monotonic() > deadline:
                raise RuntimeError(f"rule {rule_id} still readable after delete")
            time.sleep(args.poll_interval)
        result["remove_observed_seconds"] = round(time.monotonic() - started, 2)
        tests["removal_observed"] = {"passed": True}
        rule_id = ""
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        if client and rule_id:
            try:
                delete_and_wait(client, rules_path(client, args.vpc_id, rule_id), cleanup_timeout(deadline))
                rule_id = ""
            except Exception as e:
                result["cleanup_errors"] = [f"firewall_rule:{rule_id}: {e}"]
    tests["cleanup"] = {"passed": not rule_id}

    for key in KEYS:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
