#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

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
