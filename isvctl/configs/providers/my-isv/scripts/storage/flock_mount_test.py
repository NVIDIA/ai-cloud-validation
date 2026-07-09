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

"""flock mount test - TEMPLATE (replace with your platform impl).

Proves HSS14-01: the filesystem can be mounted with flock and honors advisory
file locks.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Mount the filesystem with the flock option
  2. Acquire an exclusive advisory lock (LOCK_EX) and confirm it is granted
  3. Acquire a shared advisory lock (LOCK_SH) and confirm it is granted
  4. Confirm lock contention: a conflicting lock request blocks/fails as expected
  5. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "flock_mount",
    "tests": {
      "mounted_with_flock": {"passed": true},
      "flock_exclusive":    {"passed": true},
      "flock_shared":       {"passed": true},
      "flock_contention":   {"passed": true}
    }
  }

Usage:
    python flock_mount_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the flock mount test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="flock mount test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "flock_mount",
        "tests": {
            "mounted_with_flock": {"passed": False},
            "flock_exclusive": {"passed": False},
            "flock_shared": {"passed": False},
            "flock_contention": {"passed": False},
        },
    }

    # TODO: Replace with your platform's flock mount implementation

    if DEMO_MODE:
        result["tests"] = {
            "mounted_with_flock": {"passed": True},
            "flock_exclusive": {"passed": True},
            "flock_shared": {"passed": True},
            "flock_contention": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's flock mount test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
