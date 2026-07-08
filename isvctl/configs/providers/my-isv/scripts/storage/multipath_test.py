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

"""Client multipath test - TEMPLATE (replace with your platform impl).

Proves HSS18-01: a client has multiple network paths to all storage servers.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Mount the filesystem on a client configured for multipathing
  2. Confirm the client has more than one active path (>= 2)
  3. Confirm every storage server is reachable over at least one path
  4. Fail a path and confirm I/O continues over the surviving path (failover)
  5. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "multipath",
    "tests": {
      "multiple_paths":        {"passed": true, "path_count": 2},
      "all_servers_reachable": {"passed": true, "server_count": 3},
      "failover_works":        {"passed": true}
    }
  }

Usage:
    python multipath_test.py --region <region> --servers 3
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the client multipath test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Client multipath test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--servers", type=int, default=3, help="Number of storage servers to reach")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "multipath",
        "tests": {
            "multiple_paths": {"passed": False},
            "all_servers_reachable": {"passed": False},
            "failover_works": {"passed": False},
        },
    }

    # TODO: Replace with your platform's client multipath implementation

    if DEMO_MODE:
        result["tests"] = {
            "multiple_paths": {"passed": True, "path_count": 2},
            "all_servers_reachable": {"passed": True, "server_count": args.servers},
            "failover_works": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's client multipath test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
