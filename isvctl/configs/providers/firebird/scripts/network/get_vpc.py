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

"""Read one Firebird VPC (``GET /projects/{p}/network/vpcs/{v}``).

Usage:
    python get_vpc.py --vpc-id vpc.xxx

Output JSON:
{
    "success": true,
    "platform": "network",
    "vpc_id": "vpc.xxx",
    "vpc_name": "isv-net-test-vpc",
    "cidr": "172.16.240.0/24",
    "state": "READY"
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
    """Read the VPC and emit its identity.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Get a Firebird VPC")
    parser.add_argument("--vpc-id", required=True, help="VPC ID (vpc.ULID)")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network", "vpc_id": args.vpc_id}
    try:
        client = FirebirdClient()
        vpc = client.request("GET", vpcs_path(client, args.vpc_id)).get("vpc") or {}
        if vpc.get("id") != args.vpc_id:
            raise RuntimeError(f"GET returned VPC {vpc.get('id')!r}, expected {args.vpc_id}")
        result.update({"vpc_name": vpc.get("name"), "cidr": vpc.get("cidr"), "state": vpc.get("state")})
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
