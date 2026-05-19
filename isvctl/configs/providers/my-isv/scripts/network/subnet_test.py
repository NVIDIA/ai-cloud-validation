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

"""Subnet configuration test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create a temporary VPC
  2. Create N subnets distributed across availability zones
  3. Verify route tables are associated with each subnet
  4. Clean up all resources
  5. Print a JSON object to stdout

Required JSON output fields (read by SubnetConfigCheck):
  {
    "success": true,                        # boolean - did all checks pass?
    "platform": "network",                  # string  - always "network"
    "test_name": "subnet_config",           # string  - always "subnet_config"
    "network_id": "vpc-abc123",             # string  - VPC used for the test
    "subnets": [                            # list    - created subnets
      {
        "subnet_id": "subnet-abc123",       # string  - subnet identifier
        "cidr": "10.98.0.0/24",             # string  - subnet CIDR
        "az": "<az>"                        # string  - AZ placement
      }
    ],
    "tests": {                              # object  - per-step results
      "create_vpc":         {"passed": true},
      "create_subnets":     {"passed": true,
                             "count": 4},   # int     - number of subnets created
      "az_distribution":    {"passed": true,
                             "az_count": 2,
                             "azs": ["az-a", "az-b"]},
      "subnets_available":  {"passed": true},
      "route_table_exists": {"passed": true}
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python subnet_test.py --region <region> --cidr 10.98.0.0/16 --subnet-count 4

Reference implementation: ../../aws/network/subnet_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Subnet configuration test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Subnet configuration test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", default="10.98.0.0/16", help="CIDR block for test VPC")
    parser.add_argument("--subnet-count", type=int, default=4, help="Number of subnets to create")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "subnet_config",
        "network_id": "",
        "subnets": [],
        "tests": {},
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's subnet test       ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    vpc = client.create_vpc(cidr=args.cidr)                       ║
    # ║    result["network_id"] = vpc.id                                 ║
    # ║                                                                  ║
    # ║    result["tests"]["create_vpc"] = {"passed": True}              ║
    # ║                                                                  ║
    # ║    azs = client.availability_zones()                             ║
    # ║    for i in range(args.subnet_count):                            ║
    # ║        az = azs[i % len(azs)]                                    ║
    # ║        subnet = client.create_subnet(vpc.id, az, sub_cidr)       ║
    # ║        result["subnets"].append({                                ║
    # ║            "subnet_id": subnet.id,                               ║
    # ║            "cidr": sub_cidr,                                     ║
    # ║            "az": az,                                             ║
    # ║        })                                                        ║
    # ║    result["tests"]["create_subnets"] = {"passed": True}          ║
    # ║    result["tests"]["az_distribution"] = {                        ║
    # ║        "passed": True, "az_count": len(set(azs)), "azs": azs,    ║
    # ║    }                                                             ║
    # ║                                                                  ║
    # ║    # Verify route tables                                         ║
    # ║    for s in result["subnets"]:                                   ║
    # ║        assert client.get_route_table(s["subnet_id"])             ║
    # ║    result["tests"]["route_table_exists"] = {"passed": True}      ║
    # ║    result["tests"]["subnets_available"] = {"passed": True}       ║
    # ║                                                                  ║
    # ║    # Cleanup                                                     ║
    # ║    client.delete_vpc(vpc.id, cascade=True)                       ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        azs = [f"{args.region}a", f"{args.region}b"]
        result["network_id"] = "dummy-vpc-subnet"
        for i in range(args.subnet_count):
            result["subnets"].append(
                {
                    "subnet_id": f"dummy-subnet-{i}",
                    "cidr": f"10.98.{i}.0/24",
                    "az": azs[i % len(azs)],
                }
            )
        result["tests"] = {
            "create_vpc": {"passed": True},
            "create_subnets": {"passed": True, "count": len(result["subnets"])},
            "az_distribution": {"passed": True, "az_count": len(set(azs)), "azs": azs},
            "subnets_available": {"passed": True},
            "route_table_exists": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's subnet test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
