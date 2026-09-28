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

"""Real traffic between two provisioned BMs, allowed and blocked by a firewall rule.

  traffic_allowed  the BM pings the peer (no rule in place), retried for up to
                   --removal-timeout seconds since a rule deleted by an earlier
                   check can take minutes to stop affecting traffic; if it
                   cannot, the step fails before any rule is applied
  traffic_blocked  a DENY rule for ICMP echo requests from the BM's /32 to the
                   peer's /32 is created in their VPC; once its Operation
                   completes the ping must stop answering within
                   --enforce-timeout seconds; the rule is then deleted
  internet_icmp    the BM pings --internet-host
  internet_http    the BM fetches --http-url
The internet probes need egress from the tenant subnet and fail, not skip,
without it. The rule is deleted on every path, and the blocked direction is then
polled for up to --removal-timeout seconds until it answers again, so the next
check starts on a clean data plane; ``cleanup_propagated`` records whether it
did (a timeout there is a ``cleanup_warning``, not a failure). Skips, naming
what is missing, unless both the BM and a peer BM are configured.

Usage:
    python traffic_test.py --instance-id bm.a --key-file /tmp/a --peer-id bm.b --peer-key-file /tmp/b

Output JSON:
{
    "success": true,
    "platform": "network",
    "network_id": "vpc.xxx",
    "cleanup_propagated": true,
    "tests": {"traffic_allowed": {"passed": true, "latency_ms": 0.2}, "traffic_blocked": {"passed": true},
              "internet_icmp": {"passed": true}, "internet_http": {"passed": true},
              "cleanup": {"passed": true}}
}
"""

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, remaining
from common.network import RuleGuard, cleanup_timeout, echo_request_rule, unique_name
from common.probes import pair_missing, ping, resolve_pair, run, skipped, wait_ping

KEYS = ("traffic_allowed", "traffic_blocked", "internet_icmp", "internet_http", "cleanup")


def main() -> int:
    """Probe allowed, blocked, and internet traffic and emit the traffic-flow contract.

    Returns:
        0 when every probe passed, 0 on skip, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird traffic flow test")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID)")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--peer-id", default="", help="Second provisioned BM in the same VPC (bm.ULID)")
    parser.add_argument("--peer-key-file", default="", help="SSH private key of the peer BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username on both BMs")
    parser.add_argument("--internet-host", default="8.8.8.8", help="Address pinged for internet ICMP")
    parser.add_argument("--http-url", default="http://example.com", help="URL fetched for internet HTTP")
    parser.add_argument("--enforce-timeout", type=int, default=60, help="Seconds to wait for a rule to take effect")
    parser.add_argument(
        "--removal-timeout",
        type=int,
        default=300,
        help="Seconds to wait for a deleted rule to stop affecting traffic (baseline and cleanup)",
    )
    parser.add_argument("--timeout", type=int, default=780, help="Overall timeout in seconds")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "network", "tests": tests}
    if missing := pair_missing(args):
        print(json.dumps(skipped(result, missing), indent=2))
        return 0
    deadline = time.monotonic() + args.timeout
    guard = None
    probe = None  # the (host, target) the DENY rule blocks, once it exists
    try:
        client = FirebirdClient()
        primary, peer, vpc_id = resolve_pair(client, args)
        result["network_id"] = vpc_id
        guard = RuleGuard(client, vpc_id)

        window = min(args.removal_timeout, remaining(deadline))
        ok = wait_ping(primary, peer.ip, reachable=True, timeout=window)
        tests["traffic_allowed"] = {"passed": ok, "latency_ms": ping(primary, peer.ip)[1] if ok else None}
        if not ok:
            # A DENY rule cannot be shown to block traffic that never flowed.
            raise RuntimeError(
                f"{primary.bm_id} cannot ping {peer.ip} before any rule (waited {window}s); blocking cannot be shown"
            )

        log("Blocking echo requests from the BM to the peer...")
        body = echo_request_rule(unique_name("isv-traffic"), "DENY", f"{primary.ip}/32", f"{peer.ip}/32")
        probe = (primary, peer.ip)
        rule_id = guard.create(body, remaining(deadline))
        blocked = wait_ping(primary, peer.ip, reachable=False, timeout=args.enforce_timeout)
        tests["traffic_blocked"] = {"passed": blocked}
        if not blocked:
            tests["traffic_blocked"]["error"] = f"ping still answered {args.enforce_timeout}s after the DENY rule"
        guard.delete(rule_id, remaining(deadline))

        ok, _ = ping(primary, args.internet_host)
        tests["internet_icmp"] = {"passed": ok} if ok else {"passed": False, "error": "no ICMP egress"}
        code, _ = run(primary, f"curl -fsS --max-time 10 -o /dev/null {shlex.quote(args.http_url)}")
        tests["internet_http"] = {"passed": code == 0} if code == 0 else {"passed": False, "error": "no HTTP egress"}
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        errors = guard.cleanup(cleanup_timeout(deadline)) if guard else []
        if errors:
            result["cleanup_errors"] = errors
        elif probe:
            # The rule is gone from the API; wait for the data plane so the next check
            # starts clean. Within the step's deadline, so the step timeout still holds.
            wait = min(args.removal_timeout, int(deadline - time.monotonic()))
            try:
                result["cleanup_propagated"] = wait > 0 and wait_ping(*probe, reachable=True, timeout=wait)
            except Exception as e:
                log(f"ERROR: probing the rule's removal: {e}")
                result["cleanup_propagated"] = False
            if not result["cleanup_propagated"]:
                result["cleanup_warning"] = (
                    f"the blocked direction still did not answer {max(wait, 0)}s after the rule was deleted"
                )
                log(f"WARNING: {result['cleanup_warning']}")
    tests["cleanup"] = {"passed": not result.get("cleanup_errors")}

    for key in KEYS:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
