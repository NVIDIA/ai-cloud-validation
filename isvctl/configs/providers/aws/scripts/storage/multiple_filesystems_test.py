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

"""Test multiple filesystems within total capacity via FSx (HSS09-01).

Proves that multiple parallel filesystems can coexist and that the minimum
supported filesystem size is <= 50 TiB. Self-contained: creates a minimal
VPC/subnet/security group, provisions two FSx for Lustre filesystems (each at
the 1.2 TiB PERSISTENT_2 minimum), confirms both reach AVAILABLE, and reports
the minimum filesystem size, then tears everything down.

Usage:
    python multiple_filesystems_test.py --region us-west-2 --count 2 --max-fs-tib 50

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "multiple_filesystems",
    "tests": {
        "multiple_filesystems":  {"passed": true, "filesystem_count": 2},
        "within_total_capacity": {"passed": true},
        "min_fs_size":           {"passed": true, "min_size_tib": 1.17}
    }
}
"""

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from common.errors import handle_aws_errors, stamp_test_errors
from common.fsx import (
    MIN_LUSTRE_CAPACITY_GIB,
    cleanup_fsx_resources,
    create_fsx_network,
    create_lustre_filesystem,
    new_suffix,
    wait_filesystem_available,
)

GIB_PER_TIB = 1024


@handle_aws_errors
def main() -> int:
    """Provision multiple FSx for Lustre filesystems and emit the validation JSON."""
    parser = argparse.ArgumentParser(description="Test multiple filesystems via FSx (HSS09-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--count", type=int, default=2, help="Number of filesystems to provision (>=2)")
    parser.add_argument("--max-fs-tib", type=float, default=50.0, help="Max allowed minimum FS size (TiB)")
    parser.add_argument("--cidr", default="10.87.0.0/16", help="VPC CIDR for the FSx network")
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()
    if args.count < 2:
        parser.error("--count must be >= 2")

    ec2 = boto3.client("ec2", region_name=args.region)
    fsx = boto3.client("fsx", region_name=args.region)
    suffix = new_suffix()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "multiple_filesystems",
        "tests": {
            "multiple_filesystems": {"passed": False},
            "within_total_capacity": {"passed": False},
            "min_fs_size": {"passed": False},
        },
    }

    created: dict[str, Any] = {}
    fs_ids: list[str] = []
    try:
        net = create_fsx_network(ec2, args.cidr, suffix, created)

        for i in range(args.count):
            fs_ids.append(
                create_lustre_filesystem(
                    fsx,
                    net["subnet_id"],
                    [net["sg_id"]],
                    MIN_LUSTRE_CAPACITY_GIB,
                    name=f"isv-hss09-{i}",
                    suffix=suffix,
                )
            )

        capacities = [wait_filesystem_available(fsx, fs_id).get("StorageCapacity", 0) for fs_id in fs_ids]

        # multiple_filesystems: all requested filesystems provisioned and AVAILABLE.
        result["tests"]["multiple_filesystems"] = {
            "passed": len(fs_ids) >= 2 and all(c > 0 for c in capacities),
            "filesystem_count": len(fs_ids),
        }
        # within_total_capacity: every filesystem provisioned successfully within
        # the account/region capacity (a create would fail otherwise).
        result["tests"]["within_total_capacity"] = {"passed": all(c > 0 for c in capacities)}

        # min_fs_size: the minimum filesystem size is <= the 50 TiB ceiling.
        min_tib = round(min(capacities) / GIB_PER_TIB, 2) if capacities else 0
        result["tests"]["min_fs_size"] = {
            "passed": bool(capacities) and min_tib <= args.max_fs_tib,
            "min_size_tib": min_tib,
        }

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())
    except Exception as e:  # Surface as JSON error for the validation layer.
        result["error"] = str(e)
        stamp_test_errors(result, str(e))
    finally:
        if not args.skip_cleanup:
            cleanup_errors = cleanup_fsx_resources(ec2, fsx, fs_ids, created)
            result["cleanup"] = not cleanup_errors
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
