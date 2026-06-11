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

"""List VM instances in a network/VPC.

Template stub for ISV NCP Validation. Replace the TODO section with your
platform's API calls to enumerate instances and optionally filter by ID.

This script must:
  1. Query your platform for instances in the given VPC/network
  2. Return each instance's ID and current state
  3. Optionally filter to a specific instance ID

Required JSON output fields:
  success      (bool)  - whether the operation succeeded
  platform     (str)   - always "vm"
  instances    (list)  - list of {"instance_id": str, "state": str}
  total_count  (int)   - number of instances returned
  error        (str, optional) - error message provided when success is false

Usage:
    python list_instances.py --vpc-id vpc-xxx --region <region>
    python list_instances.py --vpc-id vpc-xxx --instance-id i-xxx --region <region>

Reference implementation (AWS):
    ../aws/vm/list_instances.py
"""

import argparse
import json
import os
import sys

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """List VM instances in a VPC/network and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="List VM instances in a VPC/network")
    parser.add_argument("--vpc-id", required=True, help="VPC or network identifier")
    parser.add_argument("--instance-id", help="Filter to a specific instance ID")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "vm",
        "instances": [],
        "total_count": 0,
    }

    try:
        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's API calls     ║
        # ║                                                              ║
        # ║  1. List all instances in the VPC/network                    ║
        # ║     instances = list_instances(                              ║
        # ║         vpc_id=args.vpc_id,                                  ║
        # ║         region=args.region,                                  ║
        # ║     )                                                        ║
        # ║                                                              ║
        # ║  2. If --instance-id is provided, filter the list            ║
        # ║     if args.instance_id:                                     ║
        # ║         instances = [i for i in instances                    ║
        # ║                      if i["id"] == args.instance_id]         ║
        # ║                                                              ║
        # ║  3. Build the instances list                                 ║
        # ║     for inst in instances:                                   ║
        # ║         result["instances"].append({                         ║
        # ║             "instance_id": inst["id"],                       ║
        # ║             "state": inst["state"],                          ║
        # ║         })                                                   ║
        # ║                                                              ║
        # ║  4. Set total_count and success                              ║
        # ║     result["total_count"] = len(result["instances"])         ║
        # ║     result["success"] = True                                 ║
        # ╚══════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            target = args.instance_id or "dummy-vm-0001"
            result["instances"] = [
                {
                    "instance_id": target,
                    "state": "running",
                    "vpc_id": args.vpc_id,
                    "public_ip": "203.0.113.10",
                    "private_ip": "10.0.0.10",
                }
            ]
            result["total_count"] = len(result["instances"])
            if args.instance_id:
                result["target_instance"] = args.instance_id
                result["found_target"] = True
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's instance listing logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
