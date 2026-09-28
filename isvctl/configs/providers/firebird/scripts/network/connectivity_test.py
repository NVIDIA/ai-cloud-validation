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

"""Private-network connectivity between two provisioned BMs in one VPC.

Both BMs must be RUNNING with an API-reported IP in the same VPC; each pings the
other's IP over SSH. Skips, naming what is missing, unless both the BM and a
peer BM are configured.

Usage:
    python connectivity_test.py --instance-id bm.a --key-file /tmp/a --peer-id bm.b --peer-key-file /tmp/b

Output JSON:
{
    "success": true,
    "platform": "network",
    "network_id": "vpc.xxx",
    "instances": [{"instance_id": "bm.a", "private_ip": "172.16.240.10"},
                  {"instance_id": "bm.b", "private_ip": "172.16.240.11"}],
    "tests": {"primary_to_peer": {"passed": true, "latency_ms": 0.2},
              "peer_to_primary": {"passed": true, "latency_ms": 0.2}}
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log
from common.probes import pair_missing, ping, resolve_pair, skipped


def main() -> int:
    """Ping between the two BMs and emit the connectivity contract.

    Returns:
        0 when both directions answer, 0 on skip, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird BM-to-BM connectivity test")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID)")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--peer-id", default="", help="Second provisioned BM in the same VPC (bm.ULID)")
    parser.add_argument("--peer-key-file", default="", help="SSH private key of the peer BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username on both BMs")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "network", "instances": [], "tests": tests}
    if missing := pair_missing(args):
        print(json.dumps(skipped(result, missing), indent=2))
        return 0
    try:
        primary, peer, result["network_id"] = resolve_pair(FirebirdClient(), args)
        result["instances"] = [{"instance_id": h.bm_id, "private_ip": h.ip} for h in (primary, peer)]
        for key, source, target in (("primary_to_peer", primary, peer), ("peer_to_primary", peer, primary)):
            ok, latency = ping(source, target.ip)
            tests[key] = {"passed": ok, "latency_ms": latency}
            if not ok:
                tests[key]["error"] = f"{source.bm_id} cannot ping {target.ip}"
        result["success"] = all(t["passed"] for t in tests.values())
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
