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

"""Parallel filesystem provisioning test - TEMPLATE (replace with your impl).

Proves HSS07-01: a parallel high-speed filesystem can be provisioned via API.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Call the API to provision a parallel filesystem (e.g. Lustre/GPFS/WEKA/...)
  2. Confirm the API is reachable and the filesystem reaches a ready state
  3. Mount the filesystem on a client and confirm read/write works
  4. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "provision_parallel_fs",
    "tests": {
      "api_available":          {"passed": true},
      "filesystem_provisioned": {"passed": true, "fs_type": "parallel"},
      "mount_successful":        {"passed": true}
    }
  }

Usage:
    python provision_parallel_fs_test.py --region <region> --fs-type parallel
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the parallel filesystem provisioning test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Parallel filesystem provisioning test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--fs-type", default="parallel", help="Parallel filesystem type (e.g. lustre, gpfs, weka)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "provision_parallel_fs",
        "tests": {
            "api_available": {"passed": False},
            "filesystem_provisioned": {"passed": False},
            "mount_successful": {"passed": False},
        },
    }

    # TODO: Replace with your platform's parallel filesystem provisioning impl

    if DEMO_MODE:
        result["tests"] = {
            "api_available": {"passed": True},
            "filesystem_provisioned": {"passed": True, "fs_type": args.fs_type},
            "mount_successful": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's parallel filesystem provisioning test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
