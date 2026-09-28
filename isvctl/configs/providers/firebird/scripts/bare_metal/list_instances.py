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

"""List Firebird bare-metal servers in a VPC.

Lists the project's BMs and keeps those attached to a subnet of the given VPC.

Usage:
    python list_instances.py --vpc-id vpc.xxx [--instance-id bm.xxx]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instances": [{"instance_id": "bm.xxx", "state": "running", "vpc_id": "vpc.xxx", ...}],
    "total_count": 1,
    "target_instance": "bm.xxx",
    "found_target": true
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, to_state


def main() -> int:
    """List BMs in a VPC and optionally confirm a target BM is present.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="List Firebird BMs in a VPC")
    parser.add_argument("--vpc-id", required=True, help="VPC ID (vpc.ULID)")
    parser.add_argument("--instance-id", help="BM that must appear in the list")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instances": [],
        "total_count": 0,
    }

    try:
        client = FirebirdClient()
        subnets = client.paginate(
            f"/projects/{quote(client.project_id)}/network/vpcs/{quote(args.vpc_id)}/subnets", "items"
        )
        subnet_ids = {s["id"] for s in subnets}
        bms = client.paginate(f"/projects/{quote(client.project_id)}/compute/bms", "items")

        result["instances"] = [
            {
                "instance_id": bm["id"],
                "name": bm.get("name"),
                "state": to_state(bm),
                "instance_type": bm.get("machineTypeId"),
                "private_ip": bm.get("ipAddress"),
                "vpc_id": args.vpc_id,
                "subnet_id": bm.get("subnetId"),
            }
            for bm in bms
            if bm.get("subnetId") in subnet_ids
        ]
        result["total_count"] = len(result["instances"])

        if args.instance_id:
            result["target_instance"] = args.instance_id
            result["found_target"] = any(i["instance_id"] == args.instance_id for i in result["instances"])
            if not result["found_target"]:
                result["error"] = f"BM {args.instance_id} not found in VPC {args.vpc_id}"

        result["success"] = result.get("found_target", True)

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
