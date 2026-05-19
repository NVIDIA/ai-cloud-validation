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

"""Verify bare-metal instance terminated - TEMPLATE (replace with your platform implementation).

This script is called during the "teardown" phase, after teardown.py.
It is a post-teardown sanitization check that confirms:
  1. The instance is in "terminated" state (or no longer exists)
  2. The security group has been deleted
  3. The key pair has been deleted

Required JSON output fields:
  {
    "success": true,             # boolean - did all checks pass?
    "platform": "bm",            # string  - always "bm"
    "checks": {                  # object  - individual check results
      "instance_terminated": {
        "passed": true           # boolean - is instance terminated/gone?
      },
      "sg_deleted": {
        "passed": true           # boolean - is security group deleted?
      },
      "key_deleted": {
        "passed": true           # boolean - is key pair deleted?
      }
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python verify_terminated.py --instance-id <id> --region <region> \
        --security-group-id <sg-id> --key-name <key-name>

Reference implementation: ../../aws/bare_metal/verify_terminated.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Verify instance terminated (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Verify instance terminated (template)")
    parser.add_argument("--instance-id", required=True, help="Instance identifier")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--security-group-id", nargs="?", default=None, help="Security group to verify deleted")
    parser.add_argument("--key-name", nargs="?", default=None, help="Key pair name to verify deleted")
    args = parser.parse_args()

    # Treat empty strings (from unresolved Jinja2 templates) as None
    if args.security_group_id is not None and not args.security_group_id.strip():
        args.security_group_id = None
    if args.key_name is not None and not args.key_name.strip():
        args.key_name = None

    # Skip verification when teardown was intentionally skipped (dev workflow)
    if os.environ.get("BM_SKIP_TEARDOWN") == "true":
        print(
            json.dumps(
                {
                    "success": True,
                    "platform": "bm",
                    "instance_id": args.instance_id,
                    "message": "Verification skipped (BM_SKIP_TEARDOWN=true)",
                    "checks": {},
                },
                indent=2,
            )
        )
        return 0

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "checks": {
            "instance_terminated": {"passed": False},
            "sg_deleted": {"passed": False},
            "key_deleted": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's verify logic      ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    issues = []                                                   ║
    # ║                                                                  ║
    # ║    1. Verify instance is terminated / gone:                      ║
    # ║       try:                                                       ║
    # ║           info = client.describe_instance(args.instance_id)      ║
    # ║           if info.state in ("terminated", "deleted"):            ║
    # ║               result["checks"]["instance_terminated"]["passed"]  ║
    # ║                   = True                                         ║
    # ║           else:                                                  ║
    # ║               issues.append(f"Instance is {info.state}")         ║
    # ║       except NotFoundException:                                  ║
    # ║           result["checks"]["instance_terminated"]["passed"]      ║
    # ║               = True                                             ║
    # ║                                                                  ║
    # ║    2. Verify security group deleted:                             ║
    # ║       if args.security_group_id:                                 ║
    # ║           try:                                                   ║
    # ║               client.describe_security_group(                    ║
    # ║                   args.security_group_id                         ║
    # ║               )                                                  ║
    # ║               issues.append("SG still exists")                   ║
    # ║           except NotFoundException:                              ║
    # ║               result["checks"]["sg_deleted"]["passed"] = True    ║
    # ║                                                                  ║
    # ║    3. Verify key pair deleted:                                   ║
    # ║       if args.key_name:                                          ║
    # ║           try:                                                   ║
    # ║               client.describe_key_pair(args.key_name)            ║
    # ║               issues.append("Key pair still exists")             ║
    # ║           except NotFoundException:                              ║
    # ║               result["checks"]["key_deleted"]["passed"] = True   ║
    # ║                                                                  ║
    # ║    if not issues:                                                ║
    # ║        result["success"] = True                                  ║
    # ║    else:                                                         ║
    # ║        result["error"] = "; ".join(issues)                       ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        # Drop optional sub-checks that weren't requested so the result dict
        # only contains relevant entries (and `all()` doesn't trip on them).
        if not args.security_group_id:
            del result["checks"]["sg_deleted"]
        if not args.key_name:
            del result["checks"]["key_deleted"]

        result["checks"]["instance_terminated"]["passed"] = True
        if args.security_group_id:
            result["checks"]["sg_deleted"]["passed"] = True
        if args.key_name:
            result["checks"]["key_deleted"]["passed"] = True
        # Compute success from the included sub-checks rather than hard-coding
        # it, so a False sub-check can never coexist with success=True.
        result["success"] = all(check.get("passed") for check in result["checks"].values())
    else:
        result["error"] = "Not implemented - replace with your platform's verification logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
