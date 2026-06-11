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

"""Network connectivity test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It uses the SHARED VPC
created by create_vpc.py (passed via Jinja2 template variables):
  1. Launch instances in the provided subnets
  2. Verify each instance was assigned to the correct network / subnet
  3. Test connectivity between instances
  4. Clean up launched instances
  5. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                           # boolean - did connectivity check pass?
    "platform": "network",                     # string  - always "network"
    "test_name": "connectivity",               # string  - always "connectivity"
    "instances": [                             # list    - launched instance details
      {"instance_id": "i-abc", "subnet_id": "subnet-abc", "private_ip": "10.0.1.5"}
    ],
    "connectivity_verified": true              # boolean - instances can communicate?
  }

On failure, set "success": false and include an "error" field.

Usage:
    python test_connectivity.py --vpc-id vpc-abc123 \\
        --subnet-ids subnet-abc,subnet-def --sg-id sg-abc123 --region <region>

Reference implementation: ../../aws/network/test_connectivity.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Network connectivity test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Network connectivity test (template)")
    parser.add_argument("--vpc-id", required=True, help="VPC / network ID to test in")
    parser.add_argument("--subnet-ids", required=True, help="Comma-separated subnet IDs")
    parser.add_argument("--sg-id", required=True, help="Security group ID")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    _subnet_ids = [s.strip() for s in args.subnet_ids.split(",") if s.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "connectivity",
        "instances": [],
        "connectivity_verified": False,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's connectivity      ║
    # ║        test                                                      ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║                                                                  ║
    # ║    # Launch one instance per subnet                              ║
    # ║    instances = []                                                ║
    # ║    for sid in subnet_ids:                                        ║
    # ║        inst = client.launch_instance(                            ║
    # ║            subnet_id=sid,                                        ║
    # ║            security_group=args.sg_id,                            ║
    # ║        )                                                         ║
    # ║        instances.append(inst)                                    ║
    # ║        result["instances"].append({                              ║
    # ║            "instance_id": inst.id,                               ║
    # ║            "subnet_id": sid,                                     ║
    # ║            "private_ip": inst.private_ip,                        ║
    # ║        })                                                        ║
    # ║                                                                  ║
    # ║    # Verify connectivity between instances                       ║
    # ║    for a, b in combinations(instances, 2):                       ║
    # ║        assert client.test_connectivity(a.id, b.private_ip)       ║
    # ║    result["connectivity_verified"] = True                        ║
    # ║                                                                  ║
    # ║    # Cleanup instances (VPC remains for other tests)             ║
    # ║    for inst in instances:                                        ║
    # ║        client.terminate_instance(inst.id)                        ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["network_id"] = args.vpc_id
        result["instances"] = [
            {
                "instance_id": "dummy-conn-instance-a",
                "subnet_id": _subnet_ids[0] if _subnet_ids else "",
                "private_ip": "10.0.1.10",
                "public_ip": "203.0.113.30",
            },
            {
                "instance_id": "dummy-conn-instance-b",
                "subnet_id": _subnet_ids[-1] if _subnet_ids else "",
                "private_ip": "10.0.2.10",
            },
        ]
        result["connectivity_verified"] = True
        result["tests"] = {
            "instance_to_instance": {"passed": True},
            "instance_to_internet": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's connectivity test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
