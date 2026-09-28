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

"""List the project's Firebird VPCs (``GET /projects/{p}/network/vpcs``).

Usage:
    python list_vpcs.py [--vpc-id vpc.xxx]

Output JSON:
{
    "success": true,
    "platform": "network",
    "vpcs": [{"vpc_id": "vpc.xxx", "name": "isv-net-test-vpc", "cidr": "172.16.240.0/24", "state": "READY"}],
    "count": 1,
    "target_vpc": "vpc.xxx",
    "found_target": true
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
    """List VPCs and report whether the target VPC is among them.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="List Firebird VPCs")
    parser.add_argument("--vpc-id", default="", help="VPC that must be listed (vpc.ULID)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "vpcs": [],
        "count": 0,
        "target_vpc": args.vpc_id,
        "found_target": False,
    }
    try:
        client = FirebirdClient()
        result["vpcs"] = [
            {"vpc_id": v.get("id"), "name": v.get("name"), "cidr": v.get("cidr"), "state": v.get("state")}
            for v in client.paginate(vpcs_path(client), "items")
        ]
        result["count"] = len(result["vpcs"])
        # With no target requested, listing the project's VPCs is the whole test.
        result["found_target"] = not args.vpc_id or any(v["vpc_id"] == args.vpc_id for v in result["vpcs"])
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
