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

"""Multiple filesystems test - TEMPLATE (replace with your platform impl).

Proves HSS09-01: multiple filesystems can exist within total capacity, and the
minimum filesystem size is <= 50 TiB.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Provision two or more filesystems on the same backend
  2. Confirm their combined capacity fits within the total available capacity
  3. Confirm the minimum supported filesystem size is <= 50 TiB
  4. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "multiple_filesystems",
    "tests": {
      "multiple_filesystems":  {"passed": true, "filesystem_count": 2},
      "within_total_capacity": {"passed": true},
      "min_fs_size":           {"passed": true, "min_size_tib": 1}
    }
  }

Usage:
    python multiple_filesystems_test.py --region <region> --count 2 --max-fs-tib 50
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the multiple filesystems test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Multiple filesystems test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--count", type=int, default=2, help="Number of filesystems to provision (>=2)")
    parser.add_argument("--max-fs-tib", type=int, default=50, help="Max allowed minimum FS size (TiB)")
    args = parser.parse_args()
    if args.count < 2:
        parser.error("--count must be >= 2")

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

    # TODO: Replace with your platform's multiple-filesystem implementation

    if DEMO_MODE:
        result["tests"] = {
            "multiple_filesystems": {"passed": True, "filesystem_count": args.count},
            "within_total_capacity": {"passed": True},
            "min_fs_size": {"passed": True, "min_size_tib": 1},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's multiple-filesystem test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
