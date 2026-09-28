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

"""Delete the Firebird network pieces that create_network created.

Deletes only what create_network reported creating: each of --subnet-ids first,
then the VPC when --delete-vpc is given (a VPC cannot be deleted while it has
subnets). A supplied network is never touched, and a resource that is already
gone counts as deleted. Run it after the BM has been detached from the subnet.

Usage:
    python teardown_network.py --vpc-id vpc.xxx --subnet-ids subnet.xxx,subnet.yyy --delete-vpc
    python teardown_network.py --vpc-id vpc.xxx                  # supplied network: skipped
    python teardown_network.py --vpc-id vpc.xxx --subnet-ids subnet.xxx --skip-destroy
    python teardown_network.py ... --skip-destroy --keep-var NETWORK_SKIP_TEARDOWN \
        --reuse-vars FIREBIRD_NETWORK_VPC_ID,FIREBIRD_NETWORK_SUBNET_ID_A,FIREBIRD_NETWORK_SUBNET_ID_B

Output JSON:
{
    "success": true,
    "platform": "network",
    "resources_deleted": ["subnet:subnet.xxx", "vpc:vpc.xxx"],
    "message": "Network teardown completed"
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, remaining
from common.network import delete_and_wait, vpcs_path


def main() -> int:
    """Delete created subnets, then the created VPC.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Teardown Firebird VPC and subnets created by create_network")
    parser.add_argument("--vpc-id", default="", help="VPC the created subnets belong to (vpc.ULID)")
    parser.add_argument("--subnet-ids", default="", help="Comma-separated created subnets to delete")
    parser.add_argument("--delete-vpc", action="store_true", help="Also delete the VPC (create_network created it)")
    parser.add_argument("--skip-destroy", action="store_true", help="Keep the network")
    parser.add_argument("--keep-var", default="BM_SKIP_TEARDOWN", help="Variable that kept the network (for the hint)")
    parser.add_argument(
        "--reuse-vars",
        default="FIREBIRD_VPC_ID,FIREBIRD_SUBNET_ID",
        help="Comma-separated variables that supply a kept network: the VPC, then each subnet",
    )
    parser.add_argument("--timeout", type=int, default=1500, help="Overall timeout in seconds")
    args = parser.parse_args()
    subnet_ids = [s.strip() for s in args.subnet_ids.split(",") if s.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "resources_deleted": [],
    }

    if not subnet_ids and not args.delete_vpc:
        result["success"] = True
        result["skipped"] = True
        # A --vpc-id with nothing to delete is create_network's pass-through; no
        # --vpc-id means create_network never created anything (or never ran).
        result["skip_reason"] = (
            "Network was supplied, not created by this run; nothing to delete"
            if args.vpc_id
            else "No network was created by this run; nothing to delete"
        )
        result["message"] = result["skip_reason"]
        print(json.dumps(result, indent=2))
        return 0

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        reuse_vars = [v.strip() for v in args.reuse_vars.split(",") if v.strip()]
        values = [args.vpc_id, *subnet_ids]
        settings = " ".join(f"{var}={values[i] if i < len(values) else ''}" for i, var in enumerate(reuse_vars))
        result["skip_reason"] = (
            f"Network teardown skipped ({args.keep_var}=true). To reuse it, set {settings}; "
            "delete it afterwards through the API."
        )
        result["message"] = result["skip_reason"]
        print(json.dumps(result, indent=2))
        return 0

    deadline = time.monotonic() + args.timeout
    try:
        if not args.vpc_id:
            raise ValueError("--vpc-id is required to delete created network resources")
        client = FirebirdClient()
        vpc_path = vpcs_path(client, args.vpc_id)

        for subnet_id in subnet_ids:
            log(f"Deleting subnet {subnet_id}...")
            if not delete_and_wait(client, f"{vpc_path}/subnets/{quote(subnet_id)}", remaining(deadline)):
                log(f"  subnet {subnet_id} already deleted")
            result["resources_deleted"].append(f"subnet:{subnet_id}")

        if args.delete_vpc:
            log(f"Deleting VPC {args.vpc_id}...")
            if not delete_and_wait(client, vpc_path, remaining(deadline)):
                log(f"  VPC {args.vpc_id} already deleted")
            result["resources_deleted"].append(f"vpc:{args.vpc_id}")

        result["message"] = "Network teardown completed"
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
