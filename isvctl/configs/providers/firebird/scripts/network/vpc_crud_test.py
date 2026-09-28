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

"""VPC create/read/delete lifecycle through the Firebird VPC API (SDN01-01/02/04).

  create_vpc   POST /projects/{p}/network/vpcs, Operation awaited
  read_vpc     GET .../vpcs/{v} returns the created name and CIDR
  update_tags  not supported: a Firebird VPC has no tags
  update_dns   not supported: a Firebird VPC has no DNS settings
  delete_vpc   DELETE .../vpcs/{v}, Operation awaited, then GET returns 404

The two update operations are reported failed with a "not supported" error
rather than skipped: the API cannot do them, and a skipped subtest here would
read as a pass (PUT .../vpcs/{v} changes only the name and CIDR). The VPC is
deleted on every path once created.

Usage:
    python vpc_crud_test.py --cidr 172.16.241.0/24

Output JSON:
{
    "success": true,
    "platform": "network",
    "network_id": "vpc.xxx",
    "vpc_name": "isv-vpc-crud-a1b2c3",
    "tests": {
        "create_vpc": {"passed": true}, "read_vpc": {"passed": true},
        "update_tags": {"passed": false, "error": "not supported: ..."},
        "update_dns": {"passed": false, "error": "not supported: ..."},
        "delete_vpc": {"passed": true}
    }
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, remaining
from common.network import cleanup_timeout, delete_and_wait, exists, submit, unique_name, vpcs_path

NOT_SUPPORTED = {
    "update_tags": "not supported: Firebird VPCs have no tags (PUT updates only name and CIDR)",
    "update_dns": "not supported: Firebird VPCs have no DNS settings (PUT updates only name and CIDR)",
}


def main() -> int:
    """Run the VPC CRUD lifecycle and emit per-operation results.

    Returns:
        0 when every supported operation passed, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird VPC CRUD test")
    parser.add_argument("--name", default="isv-vpc-crud", help="Name prefix for the test VPC")
    parser.add_argument("--cidr", default="172.16.241.0/24", help="CIDR of the test VPC")
    parser.add_argument("--timeout", type=int, default=1500, help="Overall timeout in seconds")
    args = parser.parse_args()

    name = unique_name(args.name)
    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "network", "vpc_name": name, "tests": tests}
    deadline = time.monotonic() + args.timeout
    client = None
    vpc_id = ""
    try:
        client = FirebirdClient()
        log(f"Creating VPC {name} ({args.cidr})...")
        operation = submit(client, "POST", vpcs_path(client), {"name": name, "cidr": args.cidr})
        vpc_id = result["network_id"] = operation["resourceId"]
        client.wait_operation(operation, remaining(deadline))
        tests["create_vpc"] = {"passed": True}

        vpc = client.request("GET", vpcs_path(client, vpc_id)).get("vpc") or {}
        read_ok = vpc.get("id") == vpc_id and vpc.get("name") == name and vpc.get("cidr") == args.cidr
        tests["read_vpc"] = {"passed": read_ok} if read_ok else {"passed": False, "error": f"read back {vpc}"}

        for operation_name, error in NOT_SUPPORTED.items():
            tests[operation_name] = {"passed": False, "error": error}

        log(f"Deleting VPC {vpc_id}...")
        delete_and_wait(client, vpcs_path(client, vpc_id), remaining(deadline))
        gone = not exists(client, vpcs_path(client, vpc_id))
        tests["delete_vpc"] = {"passed": gone} if gone else {"passed": False, "error": "VPC still readable"}
        if gone:
            vpc_id = ""
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        if client and vpc_id:
            try:
                delete_and_wait(client, vpcs_path(client, vpc_id), cleanup_timeout(deadline))
            except Exception as e:
                result["cleanup_errors"] = [f"vpc:{vpc_id}: {e}"]

    for operation_name in ("create_vpc", "read_vpc", *NOT_SUPPORTED, "delete_vpc"):
        tests.setdefault(operation_name, {"passed": False, "error": result.get("error", "not run")})
    # The step succeeds when the operations the API supports did; VpcUpdatedCheck
    # still fails on the unsupported ones.
    result["success"] = all(t["passed"] for key, t in tests.items() if key not in NOT_SUPPORTED)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
