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

"""VPC IP configuration test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It inspects the SHARED VPC
created by create_vpc.py (VPC ID passed via Jinja2 template variables):
  1. Describe VPC DHCP options configuration
  2. Describe subnet CIDR allocations and auto-assign settings
  3. Report available IP capacity per subnet
  4. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                           # boolean - did the inspection succeed?
    "platform": "network",                     # string  - always "network"
    "test_name": "vpc_ip_config",              # string  - always "vpc_ip_config"
    "network_id": "vpc-abc123",                # string  - VPC identifier
    "cidr": "10.0.0.0/16",                     # string  - VPC CIDR block
    "subnets": [                               # list    - subnet details
      {
        "subnet_id": "subnet-abc",             # string  - subnet identifier
        "cidr": "10.0.1.0/24",                 # string  - subnet CIDR
        "az": "<az>",                          # string  - availability zone
        "auto_assign_public_ip": true,         # boolean - auto-assign public IP?
        "available_ips": 251                   # int     - available IP addresses
      }
    ],
    "dhcp_options": {                          # object  - DHCP options set
      "dhcp_options_id": "dopt-abc",           # string  - DHCP options set ID
      "domain_name": "internal.example",       # string  - domain name
      "domain_name_servers": ["..."],          # list - DNS servers
      "ntp_servers": []                        # list    - NTP servers (may be empty)
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python vpc_ip_config_test.py --vpc-id vpc-abc123 --region <region>

Reference implementation: ../../aws/network/vpc_ip_config_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """VPC IP configuration test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="VPC IP configuration test (template)")
    parser.add_argument("--vpc-id", required=True, help="VPC / network ID to inspect")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "vpc_ip_config",
        "network_id": args.vpc_id,
        "cidr": None,
        "subnets": [],
        "dhcp_options": None,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║                                                                  ║
    # ║    # Describe VPC                                                ║
    # ║    vpc = client.describe_vpc(args.vpc_id)                        ║
    # ║    result["cidr"] = vpc.cidr_block                               ║
    # ║                                                                  ║
    # ║    # Describe DHCP options                                       ║
    # ║    dhcp = client.describe_dhcp_options(vpc.dhcp_options_id)      ║
    # ║    result["dhcp_options"] = {                                    ║
    # ║        "dhcp_options_id": dhcp.id,                               ║
    # ║        "domain_name": dhcp.domain_name,                          ║
    # ║        "domain_name_servers": dhcp.dns_servers,                  ║
    # ║        "ntp_servers": dhcp.ntp_servers or [],                    ║
    # ║    }                                                             ║
    # ║                                                                  ║
    # ║    # Describe subnets                                            ║
    # ║    for subnet in client.describe_subnets(vpc_id=args.vpc_id):    ║
    # ║        result["subnets"].append({                                ║
    # ║            "subnet_id": subnet.id,                               ║
    # ║            "cidr": subnet.cidr_block,                            ║
    # ║            "az": subnet.availability_zone,                       ║
    # ║            "auto_assign_public_ip": subnet.map_public_ip,        ║
    # ║            "available_ips": subnet.available_ip_count,           ║
    # ║        })                                                        ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["cidr"] = "10.0.0.0/16"
        result["subnets"] = [
            {
                "subnet_id": "dummy-subnet-a",
                "cidr": "10.0.1.0/24",
                "az": f"{args.region}a",
                "auto_assign_public_ip": True,
                "available_ips": 251,
            },
            {
                "subnet_id": "dummy-subnet-b",
                "cidr": "10.0.2.0/24",
                "az": f"{args.region}b",
                "auto_assign_public_ip": False,
                "available_ips": 251,
            },
        ]
        result["dhcp_options"] = {
            "dhcp_options_id": "dummy-dopt",
            "domain_name": "internal.my-isv.test",
            "domain_name_servers": ["10.0.0.2"],
            "ntp_servers": [],
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's VPC inspection logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
