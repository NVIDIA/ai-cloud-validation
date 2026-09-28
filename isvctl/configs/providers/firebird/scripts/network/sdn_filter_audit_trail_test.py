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

"""Audit trail for firewall-rule changes via the Firebird audit API (SDN09-03).

Creates, modifies (PUT), and deletes a probe firewall rule, then polls
``GET /audit?targetKind=FIREWALL_RULE&targetId=<rule>`` until a CREATE, an
UPDATE, and a DELETE event appear for it. Each found event must name its actor
(``actorId``), time (``ts``), and action (``operationAction``).

The audit API is registered only when audit is enabled on the deployment; a
404 on the first read is a structured skip, taken before anything is created.

The probe rule lives in the run's network (create_network's VPC and its first
subnet, created or supplied); this step creates no network and never deletes
one. A probe rule only reaches READY when at least one prefix is inside an
existing subnet of the VPC; depending on the deployment an invalid rule is
rejected immediately or through its Operation. So the rule denies TCP from
TEST-NET-1 (RFC 5737), any source port, to one ``/32`` inside --subnet-id (its
CIDR is read from the API). No real traffic comes from TEST-NET-1, so the rule
touches none, and it is deleted on every path.

Usage:
    python sdn_filter_audit_trail_test.py --vpc-id vpc.xxx --subnet-id subnet.xxx [--audit-timeout 180]

Output JSON:
{
    "success": true,
    "platform": "network",
    "trail_id": "firebird-audit-api",
    "actor_field": "actorId",
    "target_rule_id": "firewall-rule.xxx",
    "tests": {"audit_endpoint_reachable": {"passed": true}, "create_rule_logged": {"passed": true},
              "modify_rule_logged": {"passed": true}, "delete_rule_logged": {"passed": true},
              "audit_event_has_required_fields": {"passed": true}, "cleanup": {"passed": true}}
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdApiError, FirebirdClient, is_not_registered, log, remaining
from common.network import (
    TEST_NET_SRC,
    cleanup_timeout,
    delete_and_wait,
    rules_path,
    submit,
    subnet_probe_host,
    tcp_rule,
    unique_name,
)

KEYS = (
    "audit_endpoint_reachable",
    "create_rule_logged",
    "modify_rule_logged",
    "delete_rule_logged",
    "audit_event_has_required_fields",
    "cleanup",
)
LOGGED = {"CREATE": "create_rule_logged", "UPDATE": "modify_rule_logged", "DELETE": "delete_rule_logged"}
REQUIRED_FIELDS = ("actorId", "ts", "operationAction")


def audit_events(client: FirebirdClient, rule_id: str) -> list[dict[str, Any]]:
    """Return the audit events that target ``rule_id``."""
    params = {"projectId": client.project_id, "targetKind": "FIREWALL_RULE", "targetId": rule_id}
    return client.paginate("/audit", "items", params=params)


def main() -> int:
    """Change a probe rule and find each change in the audit trail.

    Returns:
        0 when every step passed or the audit API is disabled, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird firewall-rule audit trail test")
    parser.add_argument("--vpc-id", required=True, help="VPC to add the probe rule to (vpc.ULID)")
    parser.add_argument("--subnet-id", required=True, help="Subnet of that VPC the rule targets (subnet.ULID)")
    parser.add_argument("--audit-timeout", type=int, default=180, help="Seconds to wait for the events")
    parser.add_argument("--timeout", type=int, default=780, help="Overall timeout in seconds")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "trail_id": "firebird-audit-api",
        "actor_field": "actorId",
        "tests": tests,
    }
    deadline = time.monotonic() + args.timeout
    client = None
    rule_id = ""
    try:
        client = FirebirdClient()
        try:
            client.request("GET", "/audit", params={"projectId": client.project_id, "pageSize": 1})
        except FirebirdApiError as e:
            if not is_not_registered(e):
                raise
            result.update({"success": True, "skipped": True, "skip_reason": "The audit API is not enabled"})
            print(json.dumps(result, indent=2))
            return 0
        tests["audit_endpoint_reachable"] = {"passed": True}

        target = subnet_probe_host(client, args.vpc_id, args.subnet_id)
        body = tcp_rule(unique_name("isv-audit"), "DENY", TEST_NET_SRC, target, 9)
        operation = submit(client, "POST", rules_path(client, args.vpc_id), body)
        rule_id = result["target_rule_id"] = operation["resourceId"]
        client.wait_operation(operation, remaining(deadline))
        operation = submit(client, "PUT", rules_path(client, args.vpc_id, rule_id), {"dstPortFrom": 7, "dstPortTo": 7})
        client.wait_operation(operation, remaining(deadline))
        delete_and_wait(client, rules_path(client, args.vpc_id, rule_id), remaining(deadline))
        deleted_id, rule_id = rule_id, ""

        audit_deadline = min(time.monotonic() + args.audit_timeout, deadline)
        while True:
            found = {e.get("operationAction"): e for e in audit_events(client, deleted_id)}
            if set(LOGGED) <= set(found) or time.monotonic() > audit_deadline:
                break
            time.sleep(10)
        for action, key in LOGGED.items():
            tests[key] = {"passed": action in found}
            if action not in found:
                tests[key]["error"] = f"no {action} audit event for {deleted_id} after {args.audit_timeout}s"
        incomplete = sorted(
            f"{action}:{field}" for action, e in found.items() for field in REQUIRED_FIELDS if not e.get(field)
        )
        tests["audit_event_has_required_fields"] = {"passed": bool(found) and not incomplete}
        if incomplete or not found:
            tests["audit_event_has_required_fields"]["error"] = f"missing {', '.join(incomplete) or 'events'}"
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
