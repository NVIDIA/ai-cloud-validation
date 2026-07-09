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

"""Live filesystem expansion test - TEMPLATE (replace with your platform impl).

Proves HSS10-01: a filesystem can be expanded live (capacity, inodes, I/O
throughput, and metadata) without disrupting active workloads.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Provision a filesystem and start an active I/O workload against it
  2. Expand capacity live and confirm the new capacity is visible to the client
  3. Expand the inode limit live and confirm the new inode budget is visible
  4. Confirm I/O ran uninterrupted and metadata stayed consistent across expansion
  5. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "live_expansion",
    "tests": {
      "capacity_expanded":   {"passed": true},
      "inodes_expanded":     {"passed": true},
      "io_uninterrupted":    {"passed": true},
      "metadata_consistent": {"passed": true}
    }
  }

Usage:
    python live_expansion_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the live filesystem expansion test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Live filesystem expansion test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "live_expansion",
        "tests": {
            "capacity_expanded": {"passed": False},
            "inodes_expanded": {"passed": False},
            "io_uninterrupted": {"passed": False},
            "metadata_consistent": {"passed": False},
        },
    }

    # TODO: Replace with your platform's live filesystem expansion implementation

    if DEMO_MODE:
        result["tests"] = {
            "capacity_expanded": {"passed": True},
            "inodes_expanded": {"passed": True},
            "io_uninterrupted": {"passed": True},
            "metadata_consistent": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's live filesystem expansion test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
