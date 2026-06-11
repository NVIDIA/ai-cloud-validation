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

"""Backend switch fabric metadata test - TEMPLATE.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Resolve the target compute node
  2. Query the provider's topology metadata API
  3. Return backend leaf, spine, and core switch identifiers
  4. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "backend_switch_fabric",
    "node_id": "compute-node-1",
    "fabric": {
      "leaf_switch_ids": ["leaf-1"],
      "spine_switch_ids": ["spine-1"],
      "core_switch_ids": ["core-1"]
    },
    "tests": {
      "node_resolved": {"passed": true},
      "leaf_switch_ids_present": {"passed": true},
      "spine_switch_ids_present": {"passed": true},
      "core_switch_ids_present": {"passed": true}
    }
  }

Usage:
    python backend_switch_fabric_test.py --region <region> --node-id <id>
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow importing provider-local helpers from scripts/common/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Query backend switch fabric metadata and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Backend switch fabric metadata test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--node-id", required=True, help="Compute node identifier")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "backend_switch_fabric",
        "region": args.region,
        "node_id": args.node_id,
        "fabric": {
            "leaf_switch_ids": [],
            "spine_switch_ids": [],
            "core_switch_ids": [],
        },
        "tests": {
            "node_resolved": {"passed": False},
            "leaf_switch_ids_present": {"passed": False},
            "spine_switch_ids_present": {"passed": False},
            "core_switch_ids_present": {"passed": False},
        },
    }

    # TODO: Replace with your platform's fabric topology metadata lookup.

    if DEMO_MODE:
        result["fabric"] = {
            "leaf_switch_ids": ["leaf-1"],
            "spine_switch_ids": ["spine-1"],
            "core_switch_ids": ["core-1"],
        }
        result["tests"] = {
            "node_resolved": {"passed": True},
            "leaf_switch_ids_present": {"passed": True},
            "spine_switch_ids_present": {"passed": True},
            "core_switch_ids_present": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's fabric topology lookup"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
