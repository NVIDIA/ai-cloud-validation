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

"""Delete user account - TEMPLATE (replace with your platform implementation).

This script is called during the "teardown" phase. It must:
  1. Clean up any credentials / tokens for the user
  2. Delete the user account
  3. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                     # boolean - did the delete succeed?
    "platform": "iam",                   # string  - always "iam"
    "resources_deleted": ["user:name"],  # list    - what was cleaned up
    "message": "User deleted"            # string  - human-readable status
  }

On failure, set "success": false and include an "error" field.
If the user doesn't exist, return success (idempotent teardown).

Usage:
    python delete_user.py --username test-user-abc123
    python delete_user.py --username test-user-abc123 --skip-destroy

Reference implementation: ../../aws/iam/delete_user.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Delete IAM user (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Delete IAM user (template)")
    parser.add_argument("--username", required=True, help="Username to delete")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual deletion")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "iam",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result["success"] = True
        result["message"] = "Destroy skipped (--skip-destroy flag)"
        print(json.dumps(result, indent=2))
        return 0

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's user deletion     ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyIamClient(api_url=os.environ["IAM_API_URL"])       ║
    # ║    client.revoke_all_keys(username=args.username)                ║
    # ║    client.delete_user(username=args.username)                    ║
    # ║    result["resources_deleted"].append(f"user:{args.username}")   ║
    # ║    result["success"] = True                                      ║
    # ║    result["message"] = "User deleted successfully"               ║
    # ║                                                                  ║
    # ║  If user not found, still return success (idempotent):           ║
    # ║    result["success"] = True                                      ║
    # ║    result["message"] = "User not found (already deleted)"        ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        # Demo-success output for the validation-contract demo test.
        # Safe to delete once the TODO above is filled in for real.
        result["resources_deleted"].append(f"user:{args.username}")
        result["success"] = True
        result["message"] = "User deleted successfully"
    else:
        result["error"] = "Not implemented - replace with your platform's user deletion logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
