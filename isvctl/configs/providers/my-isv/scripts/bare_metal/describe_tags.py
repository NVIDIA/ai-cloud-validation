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

"""Retrieve user-defined tags on a bare-metal instance.

Template stub for ISV NCP Validation. Replace the TODO section with your
platform's API calls to fetch instance tags/labels.

This script must:
  1. Query your platform API for the tags on the given instance
  2. Return them as a flat key->value dict in the JSON output

Required JSON output fields:
  success     (bool)  - whether the operation succeeded
  platform    (str)   - always "bm"
  instance_id (str)   - the queried instance ID
  tags        (dict)  - key/value map of all instance tags
  tag_count   (int)   - number of tags returned
  error       (str, optional) - error message when success is false

Usage:
    python describe_tags.py --instance-id <id> --region <region>

Reference implementation (AWS):
    ../aws/bare_metal/describe_tags.py
"""

import argparse
import json
import os
import sys

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Parse CLI args and emit bare-metal tag metadata as structured JSON."""
    parser = argparse.ArgumentParser(description="Describe bare-metal instance tags")
    parser.add_argument("--instance-id", required=True, help="Instance ID")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "tags": {},
        "tag_count": 0,
    }

    try:
        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's API calls     ║
        # ║                                                              ║
        # ║  1. Query your API for the instance's tags                   ║
        # ║     tags = get_instance_tags(args.instance_id, args.region)  ║
        # ║                                                              ║
        # ║  2. Populate the result dict:                                ║
        # ║     result["tags"] = tags          # dict: {key: value}      ║
        # ║     result["tag_count"] = len(tags)                          ║
        # ║     result["success"] = True                                 ║
        # ╚══════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            result["instance_id"] = args.instance_id
            result["tags"] = {
                "Name": "isv-bm-test-gpu",
                "CreatedBy": "isvtest",
            }
            result["tag_count"] = len(result["tags"])
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's tag retrieval logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
