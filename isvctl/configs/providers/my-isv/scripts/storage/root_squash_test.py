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

"""Root-squash toggle test - TEMPLATE (replace with your platform impl).

Proves HSS13-01: root-squash can be enabled and disabled at any time.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Enable root-squash on an export/filesystem at runtime
  2. Confirm a root client is mapped to the anonymous uid/gid (squashed)
  3. Disable root-squash at runtime
  4. Confirm a root client regains root privileges (unsquashed)
  5. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "root_squash",
    "tests": {
      "enable_root_squash":  {"passed": true},
      "root_squashed":       {"passed": true},
      "disable_root_squash": {"passed": true},
      "root_unsquashed":     {"passed": true}
    }
  }

Usage:
    python root_squash_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the root-squash toggle test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Root-squash toggle test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "root_squash",
        "tests": {
            "enable_root_squash": {"passed": False},
            "root_squashed": {"passed": False},
            "disable_root_squash": {"passed": False},
            "root_unsquashed": {"passed": False},
        },
    }

    # TODO: Replace with your platform's root-squash toggle implementation

    if DEMO_MODE:
        result["tests"] = {
            "enable_root_squash": {"passed": True},
            "root_squashed": {"passed": True},
            "disable_root_squash": {"passed": True},
            "root_unsquashed": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's root-squash toggle test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
