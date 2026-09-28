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

"""Check a subnet belongs to its VPC (``GET /projects/{p}/network/vpcs/{v}/subnets``).

Usage:
    python subnet_assignment.py --vpc-id vpc.xxx --subnet-id subnet.xxx

Output JSON:
{
    "success": true,
    "platform": "network",
    "subnet_count": 2,
    "tests": {"subnet_assigned": {"passed": true}}
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log
from common.network import vpcs_path


def main() -> int:
    """List the VPC's subnets and report whether the expected one is among them.

    Returns:
        0 when the subnet is assigned to the VPC, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Check a Firebird subnet belongs to its VPC")
    parser.add_argument("--vpc-id", required=True, help="VPC ID (vpc.ULID)")
    parser.add_argument("--subnet-id", required=True, help="Subnet expected in the VPC (subnet.ULID)")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network", "subnet_count": 0, "tests": {}}
    try:
        client = FirebirdClient()
        subnets = client.paginate(vpcs_path(client, args.vpc_id, "/subnets"), "items")
        result["subnet_count"] = len(subnets)
        assigned = any(s.get("id") == args.subnet_id and s.get("vpcId") == args.vpc_id for s in subnets)
        result["tests"]["subnet_assigned"] = (
            {"passed": True} if assigned else {"passed": False, "error": f"{args.subnet_id} not in {args.vpc_id}"}
        )
        result["success"] = assigned
    except Exception as e:
        result["tests"]["subnet_assigned"] = {"passed": False, "error": str(e)}
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
