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

"""Changelog/audit test - TEMPLATE (replace with your platform impl).

Proves HSS15-01: changelog/audit data is accessible, tracking by uid/gid for
file and directory operations.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Enable the filesystem changelog/audit feature
  2. Perform file operations (create/write/rename/delete) and read them back
     from the changelog
  3. Perform directory operations (mkdir/rmdir) and read them back
  4. Confirm each record attributes the operation to a uid/gid
  5. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "changelog_audit",
    "tests": {
      "changelog_enabled": {"passed": true},
      "records_file_ops":  {"passed": true},
      "records_dir_ops":   {"passed": true},
      "tracks_uid_gid":    {"passed": true}
    }
  }

Usage:
    python changelog_audit_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the changelog/audit test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Changelog/audit test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "changelog_audit",
        "tests": {
            "changelog_enabled": {"passed": False},
            "records_file_ops": {"passed": False},
            "records_dir_ops": {"passed": False},
            "tracks_uid_gid": {"passed": False},
        },
    }

    # TODO: Replace with your platform's changelog/audit implementation

    if DEMO_MODE:
        result["tests"] = {
            "changelog_enabled": {"passed": True},
            "records_file_ops": {"passed": True},
            "records_dir_ops": {"passed": True},
            "tracks_uid_gid": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's changelog/audit test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
