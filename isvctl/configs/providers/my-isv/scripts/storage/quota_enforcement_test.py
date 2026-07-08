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

"""Quota enforcement test - TEMPLATE (replace with your platform impl).

Proves HSS12-01: uid/gid/project-id soft and hard quotas are enforced.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Set uid, gid, and project-id quotas (with soft and hard limits)
  2. Write past the soft limit and confirm the grace period is honored
  3. Write past the hard limit and confirm writes are blocked (EDQUOT)
  4. Confirm enforcement is scoped correctly per uid, per gid, and per project
  5. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "quota_enforcement",
    "tests": {
      "uid_quota_enforced":     {"passed": true},
      "gid_quota_enforced":     {"passed": true},
      "project_quota_enforced": {"passed": true},
      "soft_quota_grace":       {"passed": true},
      "hard_quota_blocks":      {"passed": true}
    }
  }

Usage:
    python quota_enforcement_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the quota enforcement test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Quota enforcement test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "quota_enforcement",
        "tests": {
            "uid_quota_enforced": {"passed": False},
            "gid_quota_enforced": {"passed": False},
            "project_quota_enforced": {"passed": False},
            "soft_quota_grace": {"passed": False},
            "hard_quota_blocks": {"passed": False},
        },
    }

    # TODO: Replace with your platform's quota enforcement implementation

    if DEMO_MODE:
        result["tests"] = {
            "uid_quota_enforced": {"passed": True},
            "gid_quota_enforced": {"passed": True},
            "project_quota_enforced": {"passed": True},
            "soft_quota_grace": {"passed": True},
            "hard_quota_blocks": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's quota enforcement test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
