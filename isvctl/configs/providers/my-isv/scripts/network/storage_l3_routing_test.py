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

"""All-to-all storage L3 routing test - TEMPLATE (replace with your platform impl).

Proves SDN08-01: storage hosts spread across multiple subnets of one
software-defined private network reach every other host over L3 (full mesh),
with traffic routed on the VPC local route rather than through a gateway.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create one private network (VPC) and >=2 subnets in it
  2. Launch >=3 storage hosts spread across those subnets, all in one SG that
     allows intra-network traffic
  3. Probe the full mesh by private IP (every host reaches every other host)
  4. Verify cross-subnet pairs are reachable and every cross-subnet route is
     direct (local route, no gateway) - e.g. `ip route get` shows no "via"
  5. Clean up all resources
  6. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "storage_l3_routing",
    "tests": {
      "distinct_subnets":     {"passed": true, "subnet_count": 3},
      "all_to_all_reachable": {"passed": true, "pairs_tested": 6, "pairs_reachable": 6},
      "cross_subnet_routing": {"passed": true},
      "no_gateway_hop":       {"passed": true, "pairs_tested": 6, "pairs_direct": 6}
    }
  }

Usage:
    python storage_l3_routing_test.py --region <region> --cidr 10.86.0.0/16 --hosts 3

Reference implementation: ../../aws/network/storage_l3_routing_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the all-to-all storage L3 routing test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Storage L3 routing test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", default="10.86.0.0/16", help="Private network CIDR")
    parser.add_argument("--hosts", type=int, default=3, help="Number of storage hosts (>=3)")
    args = parser.parse_args()
    if args.hosts < 3:
        parser.error("--hosts must be >= 3")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "storage_l3_routing",
        "tests": {
            "distinct_subnets": {"passed": False},
            "all_to_all_reachable": {"passed": False},
            "cross_subnet_routing": {"passed": False},
            "no_gateway_hop": {"passed": False},
        },
    }

    # TODO: Replace with your platform's all-to-all storage L3 routing implementation

    if DEMO_MODE:
        pairs = args.hosts * (args.hosts - 1)
        result["tests"] = {
            "distinct_subnets": {"passed": True, "subnet_count": max(2, min(args.hosts, 3))},
            "all_to_all_reachable": {
                "passed": True,
                "pairs_tested": pairs,
                "pairs_reachable": pairs,
            },
            "cross_subnet_routing": {"passed": True},
            "no_gateway_hop": {"passed": True, "pairs_tested": pairs, "pairs_direct": pairs},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's storage L3 routing test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
