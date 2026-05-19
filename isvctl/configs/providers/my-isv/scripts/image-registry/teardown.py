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

"""Tear down all resources created during ISO validation.

Provider-agnostic template - replace the TODO section with your platform's
resource cleanup calls. Each resource should be deleted independently so
partial cleanup succeeds even if some deletions fail.

Required JSON output:
{
    "success":           bool       - true if all resources deleted,
    "platform":          str        - "image_registry",
    "resources_deleted": list[str]  - names/IDs of deleted resources,
    "message":           str        - human-readable summary,
    "error":             str        - (optional) error message, present when success is false
}

Usage:
    python teardown.py --instance-id i-xxx --image-id img-xxx --disk-ids disk-a,disk-b \\
        --bucket-name my-bucket --key-name my-key --security-group-id sg-xxx \\
        --instance-profile my-profile --region <region>

    Pass --skip-destroy to skip actual deletion (dry-run).

AWS reference implementation:
    ../aws/image-registry/teardown.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Tear down ISO validation resources and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Tear down ISO validation resources")
    parser.add_argument("--instance-id", default="", help="Instance to terminate")
    parser.add_argument("--image-id", default="", help="Machine image to deregister")
    parser.add_argument("--disk-ids", default="", help="Comma-separated snapshot/disk IDs to delete")
    parser.add_argument("--bucket-name", default="", help="Storage bucket to delete")
    parser.add_argument("--key-name", default="", help="Key pair to delete")
    parser.add_argument("--security-group-id", default="", help="Security group to delete")
    parser.add_argument("--instance-profile", default="", help="Instance profile to delete")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual deletion (dry-run)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result["success"] = True
        result["message"] = "Teardown skipped (--skip-destroy)"
        print(json.dumps(result, indent=2))
        return 0

    disk_ids = [s.strip() for s in args.disk_ids.split(",") if s.strip()]

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  Delete each resource, appending to resources_deleted:           ║
    # ║                                                                  ║
    # ║  1. Terminate the instance (args.instance_id)                    ║
    # ║     -> result["resources_deleted"].append("instance:<id>)        ║
    # ║  2. Deregister / delete the machine image (args.image_id)        ║
    # ║     -> result["resources_deleted"].append("image:<id>")          ║
    # ║  3. Delete disks (disk_ids)                                      ║
    # ║     -> result["resources_deleted"].append("snapshot:<id>")       ║
    # ║  4. Delete the storage bucket (args.bucket_name)                 ║
    # ║     -> result["resources_deleted"].append("bucket:<name>")       ║
    # ║  5. Delete the key pair (args.key_name)                          ║
    # ║     -> result["resources_deleted"].append("keypair:<name>")      ║
    # ║  6. Delete the security group (args.security_group_id)           ║
    # ║     -> result["resources_deleted"].append("sg:<id>")             ║
    # ║  7. Delete the instance profile (args.instance_profile)          ║
    # ║     -> result["resources_deleted"].append("profile:<name>")      ║
    # ║  8. Set result["message"] and result["success"] = True           ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        if args.instance_id:
            result["resources_deleted"].append(f"instance:{args.instance_id}")
        if args.image_id:
            result["resources_deleted"].append(f"image:{args.image_id}")
        for disk_id in disk_ids:
            result["resources_deleted"].append(f"snapshot:{disk_id}")
        if args.bucket_name:
            result["resources_deleted"].append(f"bucket:{args.bucket_name}")
        if args.key_name:
            result["resources_deleted"].append(f"keypair:{args.key_name}")
        if args.security_group_id:
            result["resources_deleted"].append(f"sg:{args.security_group_id}")
        if args.instance_profile:
            result["resources_deleted"].append(f"profile:{args.instance_profile}")
        result["message"] = "Image-registry resources deleted"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's resource teardown logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
