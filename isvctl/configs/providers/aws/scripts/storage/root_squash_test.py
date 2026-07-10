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

"""Test root-squash enable/disable via FSx (HSS13-01).

Proves that root-squash can be enabled and disabled at runtime. Self-contained:
creates a minimal VPC/subnet/security group, provisions an FSx for Lustre
filesystem with root-squash disabled, enables root-squash via UpdateFileSystem
and confirms it applies, then disables it again and confirms, before tearing
everything down.

FSx for Lustre RootSquash is formatted "UID:GID"; "0:0" disables squashing
(root retains root), and a non-zero pair (e.g. "65534:65534") maps remote root
to that anonymous uid/gid.

Usage:
    python root_squash_test.py --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "root_squash",
    "tests": {
        "enable_root_squash":  {"passed": true},
        "root_squashed":       {"passed": true, "root_squash": "65534:65534"},
        "disable_root_squash": {"passed": true},
        "root_unsquashed":     {"passed": true, "root_squash": "0:0"}
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
    wait_root_squash,
)

SQUASHED = "65534:65534"
UNSQUASHED = "0:0"


def _set_root_squash(fsx: Any, fs_id: str, value: str) -> None:
    """Update the filesystem's RootSquash setting."""
    fsx.update_file_system(
        FileSystemId=fs_id,
        LustreConfiguration={"RootSquashConfiguration": {"RootSquash": value}},
    )


@handle_aws_errors
def main() -> int:
    """Toggle FSx for Lustre root-squash on and off and emit the validation JSON."""
    parser = argparse.ArgumentParser(description="Test root-squash toggle via FSx (HSS13-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.89.0.0/16", help="VPC CIDR for the FSx network")
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    fsx = boto3.client("fsx", region_name=args.region)
    suffix = new_suffix()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "root_squash",
        "tests": {
            "enable_root_squash": {"passed": False},
            "root_squashed": {"passed": False},
            "disable_root_squash": {"passed": False},
            "root_unsquashed": {"passed": False},
        },
    }

    created: dict[str, Any] = {}
    fs_id: str | None = None
    try:
        net = create_fsx_network(ec2, args.cidr, suffix, created)
        fs_id = create_lustre_filesystem(
            fsx,
            net["subnet_id"],
            [net["sg_id"]],
            MIN_LUSTRE_CAPACITY_GIB,
            root_squash=UNSQUASHED,
            name="isv-hss13",
            suffix=suffix,
        )
        wait_filesystem_available(fsx, fs_id)

        # enable_root_squash + root_squashed
        _set_root_squash(fsx, fs_id, SQUASHED)
        result["tests"]["enable_root_squash"] = {"passed": True}
        squashed = wait_root_squash(fsx, fs_id, SQUASHED)
        result["tests"]["root_squashed"] = {"passed": squashed, "root_squash": SQUASHED}

        # disable_root_squash + root_unsquashed
        _set_root_squash(fsx, fs_id, UNSQUASHED)
        result["tests"]["disable_root_squash"] = {"passed": True}
        unsquashed = wait_root_squash(fsx, fs_id, UNSQUASHED)
        result["tests"]["root_unsquashed"] = {"passed": unsquashed, "root_squash": UNSQUASHED}

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
