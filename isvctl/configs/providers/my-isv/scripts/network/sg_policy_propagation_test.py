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

"""Security policy propagation timing test - TEMPLATE.

Measure how long a network filtering policy change takes to become effective
and visible. Replace the TODO block with your platform's policy create/remove
APIs and observation mechanism.

Required JSON output:
{
  "success": true,
  "platform": "network",
  "test_name": "sg_policy_propagation",
  "target_rule_id": "rule-xxx",
  "add_observed_seconds": 1.2,
  "remove_observed_seconds": 1.8,
  "max_propagation_seconds": 10,
  "tests": {
    "create_probe_rule": {"passed": true},
    "rule_observed": {"passed": true},
    "revoke_probe_rule": {"passed": true},
    "removal_observed": {"passed": true},
    "cleanup": {"passed": true}
  }
}
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

TEST_NAMES = [
    "create_probe_rule",
    "rule_observed",
    "revoke_probe_rule",
    "removal_observed",
    "cleanup",
]


def main() -> int:
    """Run the policy propagation timing template and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Security policy propagation timing test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--vpc-id", required=True, help="Network/VPC identifier to inspect")
    parser.add_argument("--max-propagation-seconds", type=float, default=10.0)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sg_policy_propagation",
        "max_propagation_seconds": args.max_propagation_seconds,
        "tests": {name: {"passed": False} for name in TEST_NAMES},
    }

    # TODO: Replace this block with your platform's network-policy mutation
    # and observation logic. Measure both add and remove propagation.
    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "target_rule_id": "demo-policy-rule",
                "add_observed_seconds": 1.0,
                "remove_observed_seconds": 1.5,
                "tests": {name: {"passed": True} for name in TEST_NAMES},
            }
        )
    else:
        result["error"] = "Not implemented - replace with your platform's policy propagation timing test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
