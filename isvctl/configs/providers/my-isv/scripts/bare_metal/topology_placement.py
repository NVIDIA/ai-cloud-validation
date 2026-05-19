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

"""Topology-based placement test for bare-metal - TEMPLATE.

This script validates that the platform supports topology-aware placement
for bare-metal instances (e.g., placement groups, rack-awareness, spine-leaf
topology constraints).

Required JSON output:
{
    "success": true,
    "platform": "bm",
    "instance_id": "<id>",
    "placement_supported": true,
    "availability_zone": "...",
    "placement_group": "<group-name>",
    "placement_strategy": "cluster",
    "operations": {
        "create_group":    {"passed": true},
        "verify_instance": {"passed": true},
        "describe_group":  {"passed": true},
        "delete_group":    {"passed": true}
    }
}

Usage:
    python topology_placement.py --instance-id <id> --region <region>

Reference implementation: ../../aws/bare_metal/topology_placement.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Topology-based placement test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Topology-based placement test (template)")
    parser.add_argument("--instance-id", required=True, help="Instance ID")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "placement_supported": False,
        "availability_zone": "",
        "placement_group": "",
        "placement_strategy": "",
        "operations": {
            "create_group": {"passed": False},
            "verify_instance": {"passed": False},
            "describe_group": {"passed": False},
            "delete_group": {"passed": False},
        },
    }

    # TODO: Replace with your platform's topology placement implementation
    if DEMO_MODE:
        result["instance_id"] = args.instance_id
        result["placement_supported"] = True
        result["availability_zone"] = f"{args.region}a"
        result["placement_group"] = "isv-bm-placement-test"
        result["placement_strategy"] = "cluster"
        result["operations"] = {
            "create_group": {"passed": True},
            "verify_instance": {"passed": True},
            "describe_group": {"passed": True},
            "delete_group": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's topology placement logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
