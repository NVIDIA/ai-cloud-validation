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

"""IMEX domain connectivity test - TEMPLATE.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Resolve the expected IMEX domain member nodes
  2. Query each node's IMEX domain state (e.g. `nvidia-imex-ctl -N` over SSH)
  3. Build a per-node reachability map (which peers each node observed)
  4. Print a JSON object to stdout

The validation (ImexDomainConnectivityCheck) computes pairwise mutual
connectivity itself from `reachability` - it does not trust a single
"fully connected" flag. Do not pre-aggregate that here; report what each
node actually observed, including asymmetric/one-way results if present.

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "imex_domain",
    "domain_id": "imex-0",
    "domain_state": "UP",
    "expected_members": ["compute-node-1", "compute-node-2"],
    "members": ["compute-node-1", "compute-node-2"],
    "reachability": {
      "compute-node-1": ["compute-node-2"],
      "compute-node-2": ["compute-node-1"]
    },
    "tests": {
      "domain_queried": {"passed": true}
    }
  }

Usage:
    python imex_domain_test.py --region <region> --node-ids <id1>,<id2>[,...]
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Query IMEX domain state/connectivity and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="IMEX domain connectivity test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument(
        "--node-ids",
        required=True,
        help="Comma-separated expected IMEX domain member node IDs (min 2)",
    )
    args = parser.parse_args()
    expected_members = [node_id.strip() for node_id in args.node_ids.split(",") if node_id.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_domain",
        "region": args.region,
        "domain_id": "",
        "domain_state": "",
        "expected_members": expected_members,
        "members": [],
        "reachability": {},
        "tests": {
            "domain_queried": {"passed": False},
        },
    }

    # TODO: Replace with your platform's IMEX domain query. Typically this
    # means SSH-ing into each expected member and running `nvidia-imex-ctl -N`,
    # then parsing that node's reported domain state and peer list.

    if DEMO_MODE:
        result["domain_id"] = "imex-0"
        result["domain_state"] = "UP"
        result["members"] = list(expected_members)
        result["reachability"] = {
            node_id: [peer for peer in expected_members if peer != node_id] for node_id in expected_members
        }
        result["tests"] = {"domain_queried": {"passed": True}}
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's IMEX domain query"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
