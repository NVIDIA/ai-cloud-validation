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

"""Test storage provisioning via the AWS FSx API (HSS01-01).

Proves that a high-speed storage filesystem can be provisioned via the vendor
API - here Amazon FSx for Lustre. Self-contained: creates a minimal VPC/subnet/
security group, provisions an FSx for Lustre filesystem, confirms the API is
reachable, the filesystem reaches AVAILABLE, and the provisioned capacity
matches the request, then tears everything down.

Usage:
    python provision_storage_test.py --region us-west-2 --capacity-gib 1200

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "provision_storage",
    "tests": {
        "api_available":    {"passed": true},
        "provisioned":      {"passed": true},
        "capacity_matches": {"passed": true, "capacity_gib": 1200}
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


@handle_aws_errors
def main() -> int:
    """Provision an FSx for Lustre filesystem and emit the validation JSON."""
    parser = argparse.ArgumentParser(description="Test storage provisioning via FSx API (HSS01-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--capacity-gib", type=int, default=MIN_LUSTRE_CAPACITY_GIB, help="Requested capacity (GiB)")
    parser.add_argument("--cidr", default="10.86.0.0/16", help="VPC CIDR for the FSx network")
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    fsx = boto3.client("fsx", region_name=args.region)
    suffix = new_suffix()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "provision_storage",
        "tests": {
            "api_available": {"passed": False},
            "provisioned": {"passed": False},
            "capacity_matches": {"passed": False},
        },
    }

    created: dict[str, Any] = {}
    fs_id: str | None = None
    try:
        net = create_fsx_network(ec2, args.cidr, suffix, created)

        # api_available: reaching the FSx control plane to create the filesystem.
        fs_id = create_lustre_filesystem(
            fsx, net["subnet_id"], [net["sg_id"]], args.capacity_gib, name="isv-hss01", suffix=suffix
        )
        result["tests"]["api_available"] = {"passed": True}

        # provisioned: the filesystem reaches the AVAILABLE lifecycle state.
        fs = wait_filesystem_available(fsx, fs_id)
        result["tests"]["provisioned"] = {"passed": fs.get("Lifecycle") == "AVAILABLE"}

        # capacity_matches: provisioned capacity equals the requested capacity.
        actual = fs.get("StorageCapacity")
        result["tests"]["capacity_matches"] = {
            "passed": actual == args.capacity_gib,
            "capacity_gib": actual,
        }

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())
    except Exception as e:  # Surface as JSON error for the validation layer.
        result["error"] = str(e)
        stamp_test_errors(result, str(e))
    finally:
        if not args.skip_cleanup:
            cleanup_errors = cleanup_fsx_resources(ec2, fsx, [fs_id] if fs_id else [], created)
            result["cleanup"] = not cleanup_errors
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
