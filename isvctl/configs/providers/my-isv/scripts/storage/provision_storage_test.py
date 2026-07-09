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

"""Storage provisioning test - TEMPLATE (replace with your platform impl).

Proves HSS01-01: a storage volume can be provisioned via the vendor/NCP API.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Call the vendor/NCP storage API to provision a volume of a requested size
  2. Confirm the API is reachable and the volume reaches a ready/available state
  3. Confirm the provisioned capacity matches the request
  4. Clean up the volume
  5. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "provision_storage",
    "tests": {
      "api_available":     {"passed": true},
      "provisioned":       {"passed": true},
      "capacity_matches":  {"passed": true, "capacity_gib": 100}
    }
  }

Usage:
    python provision_storage_test.py --region <region> --capacity-gib 100
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the storage provisioning test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Storage provisioning test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--capacity-gib", type=int, default=100, help="Requested volume capacity (GiB)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "provision_storage",
        "tests": {
            "api_available": {"passed": False},
            "provisioned": {"passed": False},
            "capacity_matches": {"passed": False},
        },
    }

    # TODO: Replace with your platform's storage provisioning implementation

    if DEMO_MODE:
        result["tests"] = {
            "api_available": {"passed": True},
            "provisioned": {"passed": True},
            "capacity_matches": {"passed": True, "capacity_gib": args.capacity_gib},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's storage provisioning test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
