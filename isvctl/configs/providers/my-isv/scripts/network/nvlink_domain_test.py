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

"""NVLink domain metadata test - TEMPLATE.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Resolve the target compute node
  2. Detect whether the node supports NVLink
  3. Query the provider's NVLink domain metadata when NVLink is supported
  4. Print a JSON object to stdout

Required JSON output fields for NVLink nodes:
  {
    "success": true,
    "platform": "network",
    "test_name": "nvlink_domain",
    "node_id": "compute-node-1",
    "nvlink_supported": true,
    "nvlink_domain_id": "domain-1",
    "tests": {
      "node_resolved": {"passed": true},
      "nvlink_support_detected": {"passed": true},
      "nvlink_domain_id_present": {"passed": true}
    }
  }

For non-NVLink nodes, return success with "nvlink_supported": false. The
validation will report an explicit skip for the NVLink domain check.

Usage:
    python nvlink_domain_test.py --region <region> --node-id <id>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Query NVLink domain metadata and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="NVLink domain metadata test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--node-id", required=True, help="Compute node identifier")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "nvlink_domain",
        "region": args.region,
        "node_id": args.node_id,
        "nvlink_supported": False,
        "tests": {
            "node_resolved": {"passed": False},
            "nvlink_support_detected": {"passed": False},
            "nvlink_domain_id_present": {"passed": False},
        },
    }

    # TODO: Replace with your platform's NVLink support and domain lookup.

    if DEMO_MODE:
        result["nvlink_supported"] = True
        result["nvlink_domain_id"] = "domain-1"
        result["tests"] = {
            "node_resolved": {"passed": True},
            "nvlink_support_detected": {"passed": True},
            "nvlink_domain_id_present": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's NVLink domain lookup"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
