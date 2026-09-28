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

"""Node- and subnet-scoped firewall rules between two provisioned BMs (SDN02-06/07).

Firebird VPCs have no default-deny security group and firewall rules have no
priority, so an "allow only this target" rule cannot be expressed. Scoping is
shown with a DENY of ICMP echo requests (type 8, stateless) aimed at one side:
the covered side stops answering while the uncovered side still does - echo
replies are not matched, so traffic in the other direction is unaffected.

  --scope node    rule: echo requests to the BM's /32 (from anywhere)
                  target_node_allowed  the BM still pings the peer
                  other_node_blocked   the peer can no longer ping the BM
  --scope subnet  rule: echo requests from the peer's subnet CIDR (to anywhere);
                  needs the two BMs on different subnets of one VPC, else skips
                  subnet_allowed       the BM (other subnet) still pings the peer
                  other_subnet_blocked the peer's subnet can no longer ping the BM

  create_sg       baseline: both directions ping before any rule (the VPC's rule
                  set is the security group)
  apply_*_rule    the rule is created and its Operation completes
  cleanup         the rule is deleted (on every path)
The output carries ``rule_polarity: "deny"`` and a ``probe`` per subtest naming
the direction probed, so the inverted mapping is visible. Rule enforcement is
polled for up to --enforce-timeout seconds. Skips, naming
what is missing, unless both the BM and a peer BM are configured.

Usage:
    python sg_scoping_test.py --scope node --instance-id bm.a --key-file /tmp/a \
        --peer-id bm.b --peer-key-file /tmp/b

Output JSON:
{
    "success": true,
    "platform": "network",
    "scope": "node",
    "rule_polarity": "deny",
    "tests": {"create_sg": {"passed": true, "probe": "bm.a <-> bm.b, no rule"},
              "apply_node_rule": {"passed": true, "probe": "DENY ICMP echo request 0.0.0.0/0 -> 172.16.243.10/32"},
              "target_node_allowed": {"passed": true, "probe": "bm.a -> bm.b, not covered by the DENY rule"},
              "other_node_blocked": {"passed": true, "probe": "bm.b -> bm.a, covered by the DENY rule (...)"},
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
from common.network import RuleGuard, cleanup_timeout, echo_request_rule, locate_subnet, unique_name
from common.probes import pair_missing, ping, resolve_pair, skipped, wait_ping

KEYS = {
    "node": ("create_sg", "apply_node_rule", "target_node_allowed", "other_node_blocked", "cleanup"),
    "subnet": ("create_sg", "apply_subnet_rule", "subnet_allowed", "other_subnet_blocked", "cleanup"),
}


def main() -> int:
    """Apply a scoped DENY rule and probe both sides of it.

    Returns:
        0 when the rule is scoped as expected, 0 on skip, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird firewall-rule scoping test")
    parser.add_argument("--scope", choices=sorted(KEYS), required=True, help="Rule scope to test")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID)")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--peer-id", default="", help="Second provisioned BM in the same VPC (bm.ULID)")
    parser.add_argument("--peer-key-file", default="", help="SSH private key of the peer BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username on both BMs")
    parser.add_argument("--enforce-timeout", type=int, default=60, help="Seconds to wait for a rule to take effect")
    parser.add_argument("--timeout", type=int, default=780, help="Overall timeout in seconds")
    args = parser.parse_args()

    create_key, apply_key, allowed_key, blocked_key, _ = KEYS[args.scope]
    tests: dict[str, dict[str, Any]] = {}
    # The contract keys read as allow semantics; rule_polarity and each subtest's
    # probe say what was actually applied and probed.
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "scope": args.scope,
        "rule_polarity": "deny",
        "tests": tests,
    }
    if missing := pair_missing(args):
        print(json.dumps(skipped(result, missing), indent=2))
        return 0
    deadline = time.monotonic() + args.timeout
    guard = None
    try:
        client = FirebirdClient()
        primary, peer, vpc_id = resolve_pair(client, args)
        if args.scope == "node":
            rule = echo_request_rule(unique_name("isv-node-scope"), "DENY", "0.0.0.0/0", f"{primary.ip}/32")
            covered = f"{primary.bm_id} ({primary.ip}/32)"
        else:
            if primary.subnet_id == peer.subnet_id:
                print(json.dumps(skipped(result, "Subnet scoping needs the two BMs on different subnets"), indent=2))
                return 0
            peer_cidr = locate_subnet(client, peer.subnet_id)[1].get("cidr") or ""
            rule = echo_request_rule(unique_name("isv-subnet-scope"), "DENY", peer_cidr, "0.0.0.0/0")
            covered = f"echo requests from {peer_cidr}"

        baseline = ping(primary, peer.ip)[0] and ping(peer, primary.ip)[0]
        tests[create_key] = {"passed": baseline, "probe": f"{primary.bm_id} <-> {peer.bm_id}, no rule"}
        if not baseline:
            raise RuntimeError("the BMs cannot ping each other before any rule; scoping cannot be shown")

        guard = RuleGuard(client, vpc_id)
        log(f"Applying {args.scope}-scoped DENY rule...")
        rule_id = guard.create(rule, remaining(deadline))
        tests[apply_key] = {
            "passed": True,
            "rule_id": rule_id,
            "probe": f"DENY ICMP echo request {rule['srcPrefix']} -> {rule['dstPrefix']}",
        }

        blocked = wait_ping(peer, primary.ip, reachable=False, timeout=args.enforce_timeout)
        tests[blocked_key] = {
            "passed": blocked,
            "probe": f"{peer.bm_id} -> {primary.bm_id}, covered by the DENY rule ({covered})",
        }
        if not blocked:
            tests[blocked_key]["error"] = f"peer still pinged the BM {args.enforce_timeout}s after the rule"
        allowed = ping(primary, peer.ip)[0]
        tests[allowed_key] = {
            "passed": allowed,
            "probe": f"{primary.bm_id} -> {peer.bm_id}, not covered by the DENY rule",
        }
        if not allowed:
            tests[allowed_key]["error"] = "the rule also blocked traffic it does not cover"
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        errors = guard.cleanup(cleanup_timeout(deadline)) if guard else []
        if errors:
            result["cleanup_errors"] = errors
    tests["cleanup"] = {"passed": not result.get("cleanup_errors")}

    for key in KEYS[args.scope]:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
