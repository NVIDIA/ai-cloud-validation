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

"""BYOIP test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create a VPC with a custom (BYOIP) CIDR block
  2. Verify the CIDR is set correctly
  3. Create a second VPC with a standard CIDR and verify no conflict
  4. Create a subnet within the custom CIDR range
  5. Clean up all resources
  6. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "tests": {
      "custom_cidr_create": {"passed": true, "vpc_id": "...", "cidr": "..."},
      "custom_cidr_verify": {"passed": true},
      "standard_cidr_create": {"passed": true},
      "no_conflict": {"passed": true},
      "custom_cidr_subnet": {"passed": true, "subnet_id": "..."}
    }
  }

Usage:
    python byoip_test.py --region <region> --custom-cidr 100.64.0.0/16

Reference implementation: ../../aws/network/byoip_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """BYOIP test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="BYOIP test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--custom-cidr", default="100.64.0.0/16", help="Custom CIDR to test")
    parser.add_argument("--standard-cidr", default="10.90.0.0/16", help="Standard CIDR for conflict check")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "tests": {
            "custom_cidr_create": {"passed": False},
            "custom_cidr_verify": {"passed": False},
            "standard_cidr_create": {"passed": False},
            "no_conflict": {"passed": False},
            "custom_cidr_subnet": {"passed": False},
        },
    }

    # TODO: Replace with your platform's BYOIP implementation

    if DEMO_MODE:
        result["tests"] = {
            "custom_cidr_create": {"passed": True, "vpc_id": "dummy-byoip-vpc", "cidr": args.custom_cidr},
            "custom_cidr_verify": {"passed": True},
            "standard_cidr_create": {"passed": True, "cidr": args.standard_cidr},
            "no_conflict": {"passed": True},
            "custom_cidr_subnet": {"passed": True, "subnet_id": "dummy-byoip-subnet"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's BYOIP test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
