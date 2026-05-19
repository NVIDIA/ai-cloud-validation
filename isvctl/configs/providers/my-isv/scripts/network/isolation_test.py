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

"""VPC isolation test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create two VPCs with separate CIDR blocks
  2. Verify no peering connection exists between them
  3. Verify no cross-VPC routes exist
  4. Verify security group rules don't allow cross-VPC traffic
  5. Clean up all resources
  6. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                           # boolean - did all isolation checks pass?
    "platform": "network",                     # string  - always "network"
    "test_name": "vpc_isolation",              # string  - always "vpc_isolation"
    "tests": {                                 # object  - per-test results
      "create_vpc_a":        {"passed": true},
      "create_vpc_b":        {"passed": true},
      "no_peering":          {"passed": true}, # boolean - no peering found?
      "no_cross_routes_a":   {"passed": true}, # boolean - VPC A has no route to B's CIDR
      "no_cross_routes_b":   {"passed": true}, # boolean - VPC B has no route to A's CIDR
      "sg_isolation_a":      {"passed": true}, # boolean - VPC A SGs block traffic from B
      "sg_isolation_b":      {"passed": true}  # boolean - VPC B SGs block traffic from A
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python isolation_test.py --region <region> --cidr-a 10.97.0.0/16 --cidr-b 10.96.0.0/16

Reference implementation: ../../aws/network/isolation_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """VPC isolation test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="VPC isolation test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr-a", default="10.97.0.0/16", help="CIDR block for VPC A")
    parser.add_argument("--cidr-b", default="10.96.0.0/16", help="CIDR block for VPC B")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "vpc_isolation",
        "tests": {
            "create_vpc_a": {"passed": False},
            "create_vpc_b": {"passed": False},
            "no_peering": {"passed": False},
            "no_cross_routes_a": {"passed": False},
            "no_cross_routes_b": {"passed": False},
            "sg_isolation_a": {"passed": False},
            "sg_isolation_b": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's isolation test    ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    vpc_a = client.create_vpc(cidr=args.cidr_a)                   ║
    # ║    vpc_b = client.create_vpc(cidr=args.cidr_b)                   ║
    # ║    result["tests"]["create_vpc_a"]["passed"] = True              ║
    # ║    result["tests"]["create_vpc_b"]["passed"] = True              ║
    # ║                                                                  ║
    # ║    # Check no peering                                            ║
    # ║    peerings = client.list_peerings(vpc_a.id, vpc_b.id)           ║
    # ║    result["tests"]["no_peering"]["passed"] = len(peerings)==0    ║
    # ║                                                                  ║
    # ║    # Check no cross-VPC routes (per direction)                   ║
    # ║    routes_a = client.get_route_table(vpc_a.id)                   ║
    # ║    has_a_to_b = any(r.dest == args.cidr_b for r in routes_a)     ║
    # ║    result["tests"]["no_cross_routes_a"]["passed"] = not has_a_to_b
    # ║    routes_b = client.get_route_table(vpc_b.id)                   ║
    # ║    has_b_to_a = any(r.dest == args.cidr_a for r in routes_b)     ║
    # ║    result["tests"]["no_cross_routes_b"]["passed"] = not has_b_to_a
    # ║                                                                  ║
    # ║    # Check security group isolation (per direction)              ║
    # ║    sg_a = client.get_default_sg(vpc_a.id)                        ║
    # ║    a_allows_b = any(r.cidr == args.cidr_b for r in sg_a.ingress) ║
    # ║    result["tests"]["sg_isolation_a"]["passed"] = not a_allows_b  ║
    # ║    sg_b = client.get_default_sg(vpc_b.id)                        ║
    # ║    b_allows_a = any(r.cidr == args.cidr_a for r in sg_b.ingress) ║
    # ║    result["tests"]["sg_isolation_b"]["passed"] = not b_allows_a  ║
    # ║                                                                  ║
    # ║    # Cleanup                                                     ║
    # ║    client.delete_vpc(vpc_a.id)                                   ║
    # ║    client.delete_vpc(vpc_b.id)                                   ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["vpc_a"] = {"id": "dummy-vpc-iso-a", "cidr": args.cidr_a}
        result["vpc_b"] = {"id": "dummy-vpc-iso-b", "cidr": args.cidr_b}
        result["tests"] = {
            "create_vpc_a": {"passed": True},
            "create_vpc_b": {"passed": True},
            "no_peering": {"passed": True},
            "no_cross_routes_a": {"passed": True},
            "no_cross_routes_b": {"passed": True},
            "sg_isolation_a": {"passed": True},
            "sg_isolation_b": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's VPC isolation test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
