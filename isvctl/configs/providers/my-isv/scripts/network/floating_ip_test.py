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

"""Floating IP test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create a VPC and two instances
  2. Allocate a floating/elastic IP
  3. Associate it with instance A, verify
  4. Reassociate it to instance B, measure the switch time
  5. Verify the switch completed within the time limit (default: 10s)
  6. Clean up all resources
  7. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "tests": {
      "allocate_eip": {"passed": true, "allocation_id": "...", "public_ip": "..."},
      "associate_to_a": {"passed": true},
      "verify_on_a": {"passed": true},
      "reassociate_to_b": {"passed": true, "switch_seconds": 2.3},
      "verify_on_b": {"passed": true},
      "verify_not_on_a": {"passed": true}
    }
  }

Usage:
    python floating_ip_test.py --region <region> --cidr 10.92.0.0/16

Reference implementation: ../../aws/network/floating_ip_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Floating IP test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Floating IP test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", default="10.92.0.0/16", help="CIDR for test VPC")
    parser.add_argument("--max-switch-seconds", type=int, default=10, help="Max switch time")
    args = parser.parse_args()  # noqa: F841

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "tests": {
            "allocate_eip": {"passed": False},
            "associate_to_a": {"passed": False},
            "verify_on_a": {"passed": False},
            "reassociate_to_b": {"passed": False},
            "verify_on_b": {"passed": False},
            "verify_not_on_a": {"passed": False},
        },
    }

    # TODO: Replace with your platform's floating IP implementation

    if DEMO_MODE:
        result["tests"] = {
            "allocate_eip": {"passed": True, "allocation_id": "eipalloc-demo", "public_ip": "203.0.113.99"},
            "associate_to_a": {"passed": True},
            "verify_on_a": {"passed": True},
            "reassociate_to_b": {"passed": True, "switch_seconds": 2.5},
            "verify_on_b": {"passed": True},
            "verify_not_on_a": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's floating IP test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
