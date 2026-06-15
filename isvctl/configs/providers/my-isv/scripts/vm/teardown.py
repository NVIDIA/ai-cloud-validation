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

"""Tear down a VM instance and associated resources.

Template stub for AI Cloud Validation. Replace the TODO section with your
platform's API calls to terminate an instance and clean up resources.

This script must:
  1. Terminate/delete the instance
  2. Wait for the instance to reach a terminated/deleted state
  3. Optionally delete the SSH key pair
  4. Optionally delete the security group/firewall rules

Required JSON output fields:
  success            (bool)  - whether the operation succeeded
  platform           (str)   - always "vm"
  resources_deleted  (list)  - list of resource identifiers that were deleted
  message            (str)   - human-readable summary of what was cleaned up
  error              (str, optional) - error message provided when success is false

Usage:
    python teardown.py --instance-id i-xxx --region <region> \\
        --delete-key-pair --delete-security-group

Reference implementation (AWS):
    ../aws/vm/teardown.py
"""

import argparse
import json
import os
import sys

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Tear down VM instance and resources and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Tear down VM instance and resources")
    parser.add_argument("--instance-id", required=True, help="Instance ID to terminate")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument(
        "--delete-key-pair",
        action="store_true",
        help="Also delete the SSH key pair",
    )
    parser.add_argument(
        "--delete-security-group",
        action="store_true",
        help="Also delete the security group",
    )
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "vm",
        "resources_deleted": [],
        "message": "",
    }

    try:
        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's API calls     ║
        # ║                                                              ║
        # ║  1. Terminate the instance                                   ║
        # ║     terminate_instance(args.instance_id, region=args.region) ║
        # ║     result["resources_deleted"].append(args.instance_id)     ║
        # ║                                                              ║
        # ║  2. Wait for the instance to be fully terminated             ║
        # ║     wait_for_terminated(args.instance_id)                    ║
        # ║                                                              ║
        # ║  3. If --delete-key-pair, delete the SSH key pair            ║
        # ║     if args.delete_key_pair:                                 ║
        # ║         delete_key_pair(key_name)                            ║
        # ║         result["resources_deleted"].append(key_name)         ║
        # ║                                                              ║
        # ║  4. If --delete-security-group, delete the security group    ║
        # ║     if args.delete_security_group:                           ║
        # ║         delete_security_group(sg_id)                         ║
        # ║         result["resources_deleted"].append(sg_id)            ║
        # ║                                                              ║
        # ║  5. Populate result                                          ║
        # ║     result["message"] = "Instance and resources deleted"     ║
        # ║     result["success"] = True                                 ║
        # ╚══════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            result["resources_deleted"].append(f"instance:{args.instance_id}")
            if args.delete_key_pair:
                result["resources_deleted"].append("key_pair:dummy-key")
            if args.delete_security_group:
                result["resources_deleted"].append("security_group:dummy-sg-0001")
            result["message"] = "VM instance and associated resources deleted"
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's teardown logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
