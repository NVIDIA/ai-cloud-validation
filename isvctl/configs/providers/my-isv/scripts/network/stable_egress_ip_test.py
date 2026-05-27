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

"""Stable egress IP test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create a test instance with outbound internet access
  2. Probe its egress IP N times against an external IP-discovery endpoint
     (e.g., https://api.ipify.org)
  3. Verify every probe returned the same address
  4. Clean up all resources
  5. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "stable_egress_ip",
    "tests": {
      "create_instance":  {"passed": true},
      "probe_egress_ip":  {"passed": true, "probes": N},
      "egress_ip_stable": {"passed": true}
    }
  }

Usage:
    python stable_egress_ip_test.py --region <region> --cidr 10.92.0.0/16 \\
        --probes 3 --interval-seconds 2 --endpoint https://api.ipify.org

Reference implementation: ../../aws/network/stable_egress_ip_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Stable egress IP test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Stable egress IP test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", default="10.92.0.0/16", help="CIDR for test VPC")
    parser.add_argument("--probes", type=int, default=3, help="Number of egress IP probes")
    parser.add_argument("--interval-seconds", type=float, default=2.0, help="Delay between probes")
    parser.add_argument(
        "--endpoint",
        default="https://api.ipify.org",
        help="IP-discovery endpoint that echoes the caller's egress IP",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "stable_egress_ip",
        "tests": {
            "create_instance": {"passed": False},
            "probe_egress_ip": {"passed": False},
            "egress_ip_stable": {"passed": False},
        },
    }

    # TODO: Replace with your platform's stable egress IP implementation

    if DEMO_MODE:
        result["tests"] = {
            "create_instance": {"passed": True},
            "probe_egress_ip": {"passed": True, "probes": args.probes},
            "egress_ip_stable": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's stable egress IP test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
