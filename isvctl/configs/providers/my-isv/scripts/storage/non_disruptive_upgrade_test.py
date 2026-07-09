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

"""Non-disruptive upgrade test - TEMPLATE (replace with your platform impl).

Proves HSS05-01: storage upgrades are non-disruptive and maintenance can be
deferred (NVIDIA can defer maintenance up to 2 weeks).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Query the upgrade/maintenance API for a pending or simulated upgrade
  2. Confirm maintenance can be deferred by at least the required window (14 days)
  3. Drive I/O against a mounted volume while the upgrade proceeds
  4. Confirm I/O continuity (no interruption/errors) across the upgrade
  5. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "non_disruptive_upgrade",
    "tests": {
      "upgrade_available":       {"passed": true},
      "io_continuity":           {"passed": true},
      "maintenance_deferrable":  {"passed": true, "max_defer_days": 14}
    }
  }

Usage:
    python non_disruptive_upgrade_test.py --region <region> --min-defer-days 14
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the non-disruptive upgrade test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Non-disruptive upgrade test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--min-defer-days", type=int, default=14, help="Required maintenance-defer window (days)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "non_disruptive_upgrade",
        "tests": {
            "upgrade_available": {"passed": False},
            "io_continuity": {"passed": False},
            "maintenance_deferrable": {"passed": False},
        },
    }

    # TODO: Replace with your platform's non-disruptive upgrade implementation

    if DEMO_MODE:
        result["tests"] = {
            "upgrade_available": {"passed": True},
            "io_continuity": {"passed": True},
            "maintenance_deferrable": {"passed": True, "max_defer_days": args.min_defer_days},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's non-disruptive upgrade test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
