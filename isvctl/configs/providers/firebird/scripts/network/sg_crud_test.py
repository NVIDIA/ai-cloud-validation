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

"""Security-group CRUD mapped onto Firebird VPC firewall rules (SDN02-01..04).

Firebird has no security-group object: a VPC's firewall rules
(``/projects/{p}/network/vpcs/{v}/firewall-rules``) are its security policy. So
the "security group" is the set of rules this step creates in the run's VPC
(create_network's VPC, created or supplied), and:

  create_vpc             the rule set's container: the run's VPC reads back READY
                         and holds --subnet-id (this step creates no VPC)
  create_sg              POST the first rule, Operation awaited, readback READY
  read_sg                GET that rule returns the fields it was created with
  update_sg_add_rule     POST a second rule; the VPC's rule list shows both
  update_sg_modify_rule  PUT the first rule's destination port; readback shows it
  update_sg_remove_rule  DELETE the second rule; the list no longer has it
  delete_sg              DELETE the remaining rule; none of the step's rules is listed
  verify_deleted         GET the deleted rule returns 404 and none is listed

The step creates no network and never deletes one: every subnet a run creates
takes a VLAN from the tenant's pool. A probe rule only reaches READY when
at least one prefix is inside an existing subnet of the VPC (the VPC CIDR
alone is not enough); depending on the deployment an invalid rule is rejected
immediately or through its Operation. So the rules match TCP from TEST-NET-1
(RFC 5737), any source port, to one ``/32`` inside --subnet-id (its CIDR is
read from the API). No real traffic comes from TEST-NET-1, so they touch none.
Rules the VPC already had are left alone, and every rule this step created is
deleted on every path.

Usage:
    python sg_crud_test.py --vpc-id vpc.xxx --subnet-id subnet.xxx

Output JSON:
{
    "success": true,
    "platform": "network",
    "network_id": "vpc.xxx",
    "tests": {"create_vpc": {"passed": true}, "create_sg": {"passed": true}, ...}
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
    RuleGuard,
    cleanup_timeout,
    exists,
    rules_path,
    submit,
    subnet_probe_host,
    tcp_rule,
    unique_name,
    vpcs_path,
)

OPERATIONS = (
    "create_vpc",
    "create_sg",
    "read_sg",
    "update_sg_add_rule",
    "update_sg_modify_rule",
    "update_sg_remove_rule",
    "delete_sg",
    "verify_deleted",
)
READ_BACK_FIELDS = ("name", "action", "protocol", "srcPrefix", "dstPrefix", "srcPortFrom", "srcPortTo", "dstPortFrom")


def main() -> int:
    """Run the firewall-rule lifecycle and emit per-operation results.

    Returns:
        0 when every operation passed, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird firewall-rule (security group) CRUD test")
    parser.add_argument("--vpc-id", required=True, help="VPC to create the rules in (vpc.ULID)")
    parser.add_argument("--subnet-id", required=True, help="Subnet of that VPC the rules target (subnet.ULID)")
    parser.add_argument("--name", default="isv-sg-crud", help="Name prefix for the rules")
    parser.add_argument("--timeout", type=int, default=1500, help="Overall timeout in seconds")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "network", "network_id": args.vpc_id, "tests": tests}
    deadline = time.monotonic() + args.timeout
    vpc_id = args.vpc_id
    guard: RuleGuard | None = None

    try:
        client = FirebirdClient()
        guard = RuleGuard(client, vpc_id)

        def listed() -> set[str]:
            """Return the IDs in the VPC's rule list."""
            return {r.get("id") for r in client.paginate(rules_path(client, vpc_id), "items")}

        vpc = client.request("GET", vpcs_path(client, vpc_id)).get("vpc") or {}
        target = subnet_probe_host(client, vpc_id, args.subnet_id)
        tests["create_vpc"] = {"passed": vpc.get("state") == "READY"}
        if vpc.get("state") != "READY":
            tests["create_vpc"]["error"] = f"VPC {vpc_id} is {vpc.get('state')}, expected READY"

        name = unique_name(args.name)
        first = tcp_rule(f"{name}-a", "ALLOW", TEST_NET_SRC, target, 8443)
        first_id = guard.create(first, remaining(deadline))
        rule = client.request("GET", rules_path(client, vpc_id, first_id)).get("firewallRule") or {}
        tests["create_sg"] = {"passed": rule.get("state") == "READY", "rule_id": first_id}
        if rule.get("state") != "READY":
            tests["create_sg"]["error"] = f"rule is {rule.get('state')}, expected READY"

        expected = {k: first[k] for k in READ_BACK_FIELDS}
        mismatched = sorted(k for k, v in expected.items() if rule.get(k) != v)
        tests["read_sg"] = {"passed": not mismatched}
        if mismatched:
            tests["read_sg"]["error"] = f"read back differs in {', '.join(mismatched)}"

        second_id = guard.create(tcp_rule(f"{name}-b", "ALLOW", TEST_NET_SRC, target, 9443), remaining(deadline))
        tests["update_sg_add_rule"] = {"passed": {first_id, second_id} <= listed()}

        operation = submit(
            client, "PUT", rules_path(client, vpc_id, first_id), {"dstPortFrom": 8444, "dstPortTo": 8444}
        )
        client.wait_operation(operation, remaining(deadline))
        rule = client.request("GET", rules_path(client, vpc_id, first_id)).get("firewallRule") or {}
        tests["update_sg_modify_rule"] = {"passed": rule.get("dstPortFrom") == 8444 and rule.get("dstPortTo") == 8444}

        guard.delete(second_id, remaining(deadline))
        now = listed()
        tests["update_sg_remove_rule"] = {"passed": second_id not in now and first_id in now}

        guard.delete(first_id, remaining(deadline))
        tests["delete_sg"] = {"passed": not {first_id, second_id} & listed()}

        gone = not exists(client, rules_path(client, vpc_id, first_id)) and first_id not in listed()
        tests["verify_deleted"] = {"passed": gone}
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        if guard and (cleanup_errors := guard.cleanup(cleanup_timeout(deadline))):
            result["cleanup_errors"] = cleanup_errors

    for operation_name in OPERATIONS:
        tests.setdefault(operation_name, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values()) and not result.get("cleanup_errors")
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
