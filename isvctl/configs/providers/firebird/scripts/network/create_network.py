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

"""Create (or pass through) the Firebird VPC and subnet a test run uses.

With --subnet-id the network is supplied: nothing is created and the IDs are
passed through, so later steps always read the network from this step. Repeat
--subnet-id to supply several subnets of the VPC (empty values are ignored).
Otherwise the missing pieces are created through the API and each asynchronous
Operation is awaited:
  - no --vpc-id:  POST /projects/{p}/network/vpcs, then the subnet(s) inside it
  - --vpc-id:     only the subnet(s), inside the supplied VPC
Created IDs are recorded before each wait, so teardown_network deletes exactly
what this run created even when a later wait fails.

VPC CIDRs must be RFC 1918 space accepted by the API (currently 172.16.0.0/16
or 192.168.0.0/16, plus operator-configured ranges) with a /8, /16 or /24
prefix; the API validates this. Each subnet's gateway is its first usable host.

Usage:
    python create_network.py --name isv-bm-test --vpc-cidr 172.16.240.0/24 --subnet-cidr 172.16.240.0/25
    python create_network.py --vpc-id vpc.xxx --subnet-id subnet.xxx     # supplied: no-op
    python create_network.py --vpc-id vpc.xxx --subnet-id subnet.a --subnet-id subnet.b

Output JSON:
{
    "success": true,
    "platform": "network",
    "network_id": "vpc.xxx",
    "cidr": "172.16.240.0/24",
    "subnets": [{"subnet_id": "subnet.xxx", "cidr": "172.16.240.0/25"}],
    "created_vpc": true,
    "created_subnet_ids": ["subnet.xxx"]
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
from common.network import gateway_ip, submit, vpcs_path


def main() -> int:
    """Create the missing VPC/subnet pieces, or pass a supplied network through.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Create Firebird VPC and subnet")
    parser.add_argument("--name", default="isv-test", help="Name prefix for created resources")
    parser.add_argument("--vpc-id", default="", help="Existing VPC (vpc.ULID); created when empty")
    parser.add_argument(
        "--subnet-id",
        action="append",
        default=[],
        help="Existing subnet (subnet.ULID); repeat for several. When set nothing is created",
    )
    parser.add_argument("--vpc-cidr", default="172.16.240.0/24", help="CIDR of a created VPC")
    parser.add_argument(
        "--subnet-cidr",
        action="append",
        help="CIDR of a created subnet; repeat for several (default: 172.16.240.0/25)",
    )
    parser.add_argument(
        "--reused-bm-id",
        default="",
        help="BM the run reuses instead of provisioning; it needs its existing network supplied",
    )
    parser.add_argument("--timeout", type=int, default=1500, help="Overall timeout in seconds")
    args = parser.parse_args()
    subnet_cidrs = args.subnet_cidr or ["172.16.240.0/25"]
    subnet_ids = [s.strip() for s in args.subnet_id if s.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "network_id": args.vpc_id,
        "subnets": [],
        "created_vpc": False,
        "created_subnet_ids": [],
    }

    deadline = time.monotonic() + args.timeout
    try:
        if subnet_ids:
            if not args.vpc_id:
                raise ValueError("--vpc-id (FIREBIRD_VPC_ID) is required with --subnet-id (FIREBIRD_SUBNET_ID)")
            result["subnets"] = [{"subnet_id": subnet_id} for subnet_id in subnet_ids]
            result["message"] = (
                f"Using supplied subnet(s) {', '.join(subnet_ids)} in VPC {args.vpc_id}; nothing created"
            )
            result["success"] = True
            print(json.dumps(result, indent=2))
            return 0

        if args.reused_bm_id:
            # A reused BM keeps the subnet it is already on; a fresh network would
            # not contain it and later VPC-scoped checks would miss it.
            raise ValueError(
                "BM_INSTANCE_ID reuse needs the BM's existing network: set FIREBIRD_VPC_ID and FIREBIRD_SUBNET_ID"
            )

        gateways = [gateway_ip(cidr) for cidr in subnet_cidrs]
        client = FirebirdClient()
        vpc_id = args.vpc_id
        if not vpc_id:
            log(f"Creating VPC {args.name}-vpc ({args.vpc_cidr})...")
            operation = submit(client, "POST", vpcs_path(client), {"name": f"{args.name}-vpc", "cidr": args.vpc_cidr})
            vpc_id = operation["resourceId"]
            result["network_id"] = vpc_id
            result["created_vpc"] = True
            client.wait_operation(operation, remaining(deadline))
            result["cidr"] = args.vpc_cidr

        subnets_path = vpcs_path(client, vpc_id, "/subnets")
        for index, (cidr, gateway) in enumerate(zip(subnet_cidrs, gateways, strict=True), start=1):
            name = f"{args.name}-subnet-{index}"
            log(f"Creating subnet {name} ({cidr}, gateway {gateway})...")
            operation = submit(client, "POST", subnets_path, {"name": name, "cidr": cidr, "gatewayIp": gateway})
            subnet_id = operation["resourceId"]
            result["created_subnet_ids"].append(subnet_id)
            client.wait_operation(operation, remaining(deadline))
            result["subnets"].append({"subnet_id": subnet_id, "cidr": cidr})

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
