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

"""Teardown bare-metal instance and resources - TEMPLATE (replace with your platform implementation).

This script is called during the "teardown" phase. It must:
  1. Check if --skip-destroy is set (or BM_SKIP_TEARDOWN env var); if so, skip
  2. Terminate the bare-metal instance (longer wait than VM for full shutdown)
  3. Optionally delete the key pair (if --delete-key-pair is set)
  4. Optionally delete the security group (if --delete-security-group is set)
  5. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                              # boolean - did teardown succeed?
    "platform": "bm",                             # string  - always "bm"
    "resources_deleted": ["instance:i-xxx", ...], # list    - what was cleaned up
    "message": "Teardown completed"               # string  - human-readable status
  }

On failure, set "success": false and include an "error" field.
If resources don't exist, return success (idempotent teardown).

Usage:
    python teardown.py --instance-id <id> --region <region> --delete-key-pair --delete-security-group
    python teardown.py --instance-id <id> --region <region> --skip-destroy

    # Also supports env var:
    BM_SKIP_TEARDOWN=true python teardown.py --instance-id <id> --region <region>

Reference implementation: ../../aws/bare_metal/teardown.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Teardown bare-metal instance and associated resources, emitting JSON result."""
    parser = argparse.ArgumentParser(description="Teardown bare-metal instance (template)")
    parser.add_argument("--instance-id", required=True, help="Instance identifier")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--delete-key-pair", action="store_true", help="Also delete key pair")
    parser.add_argument("--delete-security-group", action="store_true", help="Also delete security group")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual destruction")
    parser.add_argument(
        "--key-pair-name", default="", help="Key pair name to delete (required when --delete-key-pair is set)"
    )
    parser.add_argument(
        "--security-group-id",
        default="",
        help="Security group ID to delete (required when --delete-security-group is set)",
    )
    args = parser.parse_args()

    if args.delete_key_pair and not args.key_pair_name:
        parser.error("--key-pair-name is required when --delete-key-pair is set")
    if args.delete_security_group and not args.security_group_id:
        parser.error("--security-group-id is required when --delete-security-group is set")

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "resources_deleted": [],
        "message": "",
    }

    # Support env var override for skip-destroy
    if args.skip_destroy or os.environ.get("BM_SKIP_TEARDOWN") == "true":
        result["success"] = True
        result["message"] = (
            f"Teardown skipped. Instance {args.instance_id} is still running. "
            f"To teardown later, unset BM_SKIP_TEARDOWN and rerun."
        )
        print(json.dumps(result, indent=2))
        return 0

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's teardown logic    ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║                                                                  ║
    # ║    1. Terminate bare-metal instance (longer wait for BM):        ║
    # ║       client.terminate_instance(args.instance_id)                ║
    # ║       client.wait_until_terminated(                              ║
    # ║           args.instance_id, timeout=1800                         ║
    # ║       )                                                          ║
    # ║       result["resources_deleted"].append(                        ║
    # ║           f"instance:{args.instance_id}"                         ║
    # ║       )                                                          ║
    # ║                                                                  ║
    # ║    2. Delete key pair (if --delete-key-pair):                    ║
    # ║       if args.delete_key_pair:                                   ║
    # ║           client.delete_key_pair(args.key_pair_name)             ║
    # ║           result["resources_deleted"].append(                    ║
    # ║               f"key:{args.key_pair_name}")                       ║
    # ║                                                                  ║
    # ║    3. Delete security group (if --delete-security-group):        ║
    # ║       if args.delete_security_group:                             ║
    # ║           client.delete_security_group(args.security_group_id)   ║
    # ║           result["resources_deleted"].append(                    ║
    # ║               f"sg:{args.security_group_id}")                    ║
    # ║                                                                  ║
    # ║    result["success"] = True                                      ║
    # ║    result["message"] = "Bare-metal instance teardown completed"  ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["resources_deleted"].append(f"instance:{args.instance_id}")
        if args.delete_key_pair:
            result["resources_deleted"].append("key_pair:dummy-bm-key")
        if args.delete_security_group:
            result["resources_deleted"].append("security_group:dummy-sg-bm-0001")
        result["message"] = "Bare-metal instance and associated resources deleted"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's instance teardown logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
