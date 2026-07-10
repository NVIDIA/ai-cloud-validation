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

"""Storage QoS throughput test - TEMPLATE (replace with your platform impl).

Proves HSS02-01: provisioned throughput meets the requested minimum bandwidth
and IOPS.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Provision a volume with a QoS class (min bandwidth + min IOPS)
  2. Run a bandwidth benchmark and an IOPS benchmark against a mounted volume
  3. Confirm measured bandwidth >= requested minimum bandwidth
  4. Confirm measured IOPS >= requested minimum IOPS
  5. Clean up and print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "qos_throughput",
    "tests": {
      "bandwidth_meets_min": {"passed": true, "measured_mbps": 1200, "min_mbps": 1000},
      "iops_meets_min":      {"passed": true, "measured_iops": 60000, "min_iops": 50000}
    }
  }

Usage:
    python qos_throughput_test.py --region <region> --min-mbps 1000 --min-iops 50000
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the QoS throughput test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Storage QoS throughput test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--min-mbps", type=int, default=1000, help="Requested minimum bandwidth (MB/s)")
    parser.add_argument("--min-iops", type=int, default=50000, help="Requested minimum IOPS")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "qos_throughput",
        "tests": {
            "bandwidth_meets_min": {"passed": False},
            "iops_meets_min": {"passed": False},
        },
    }

    # TODO: Replace with your platform's QoS benchmark implementation

    if DEMO_MODE:
        result["tests"] = {
            "bandwidth_meets_min": {
                "passed": True,
                "measured_mbps": int(args.min_mbps * 1.2),
                "min_mbps": args.min_mbps,
            },
            "iops_meets_min": {
                "passed": True,
                "measured_iops": int(args.min_iops * 1.2),
                "min_iops": args.min_iops,
            },
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's QoS throughput test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
