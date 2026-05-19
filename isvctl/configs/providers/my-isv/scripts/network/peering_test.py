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

"""VPC peering test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create two VPCs with separate CIDRs
  2. Create a peering connection between them
  3. Accept the peering connection
  4. Add routes in both VPCs pointing to each other via the peering
  5. Verify the peering is in active state
  6. Clean up all resources
  7. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "tests": {
      "create_vpc_a": {"passed": true, "vpc_id": "..."},
      "create_vpc_b": {"passed": true, "vpc_id": "..."},
      "create_peering": {"passed": true, "peering_id": "..."},
      "accept_peering": {"passed": true},
      "add_routes": {"passed": true},
      "peering_active": {"passed": true, "status": "active"}
    }
  }

Usage:
    python peering_test.py --region <region> --cidr-a 10.88.0.0/16 --cidr-b 10.87.0.0/16

Reference implementation: ../../aws/network/peering_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """VPC peering test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="VPC peering test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr-a", default="10.88.0.0/16", help="CIDR for VPC A")
    parser.add_argument("--cidr-b", default="10.87.0.0/16", help="CIDR for VPC B")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "tests": {
            "create_vpc_a": {"passed": False},
            "create_vpc_b": {"passed": False},
            "create_peering": {"passed": False},
            "accept_peering": {"passed": False},
            "add_routes": {"passed": False},
            "peering_active": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's VPC peering       ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client  = MyNetworkClient(region=args.region)                 ║
    # ║    vpc_a   = client.create_vpc(cidr=args.cidr_a)                 ║
    # ║    vpc_b   = client.create_vpc(cidr=args.cidr_b)                 ║
    # ║    result["vpc_a"] = {"id": vpc_a.id, "cidr": args.cidr_a}       ║
    # ║    result["vpc_b"] = {"id": vpc_b.id, "cidr": args.cidr_b}       ║
    # ║    peering = client.create_peering(vpc_a.id, vpc_b.id)           ║
    # ║    result["tests"]["create_peering"] = {                         ║
    # ║        "passed": True, "peering_id": peering.id}                 ║
    # ║    client.accept_peering(peering.id)                             ║
    # ║    result["tests"]["accept_peering"] = {"passed": True}          ║
    # ║    client.add_route(vpc_a.route_table, args.cidr_b, peering.id)  ║
    # ║    client.add_route(vpc_b.route_table, args.cidr_a, peering.id)  ║
    # ║    result["tests"]["add_routes"] = {"passed": True}              ║
    # ║    assert client.get_peering(peering.id).status == "active"      ║
    # ║    result["tests"]["peering_active"] = {"passed": True}          ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["vpc_a"] = {"id": "dummy-vpc-peer-a", "cidr": args.cidr_a}
        result["vpc_b"] = {"id": "dummy-vpc-peer-b", "cidr": args.cidr_b}
        result["tests"] = {
            "create_vpc_a": {"passed": True, "vpc_id": result["vpc_a"]["id"]},
            "create_vpc_b": {"passed": True, "vpc_id": result["vpc_b"]["id"]},
            "create_peering": {"passed": True, "peering_id": "dummy-peer"},
            "accept_peering": {"passed": True},
            "add_routes": {"passed": True},
            "peering_active": {"passed": True, "status": "active"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's VPC peering test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
