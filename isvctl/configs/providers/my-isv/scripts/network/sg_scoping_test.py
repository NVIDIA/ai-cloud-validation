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

"""Security group scoping test - TEMPLATE (replace with your platform implementation).

Tests that security group rules can be scoped at a specific granularity
level (workload, node, subnet/tenant, or service). The --scope flag
selects which level to test.

Required JSON output fields vary by scope:

  scope=workload:
    tests: {create_sg, apply_workload_rule, workload_allowed,
            other_workload_blocked, cleanup}

  scope=node:
    tests: {create_sg, apply_node_rule, target_node_allowed,
            other_node_blocked, cleanup}

  scope=subnet:
    tests: {create_sg, apply_subnet_rule, subnet_allowed,
            other_subnet_blocked, cleanup}

  scope=service:
    tests: {create_sg, apply_service_rule, service_endpoint_allowed,
            other_endpoint_blocked, cleanup}

Usage:
    python sg_scoping_test.py --region <region> --scope workload
    python sg_scoping_test.py --region <region> --scope node
    python sg_scoping_test.py --region <region> --scope subnet
    python sg_scoping_test.py --region <region> --scope service
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

SCOPE_TESTS: dict[str, list[str]] = {
    "workload": [
        "create_sg",
        "apply_workload_rule",
        "workload_allowed",
        "other_workload_blocked",
        "cleanup",
    ],
    "node": [
        "create_sg",
        "apply_node_rule",
        "target_node_allowed",
        "other_node_blocked",
        "cleanup",
    ],
    "subnet": [
        "create_sg",
        "apply_subnet_rule",
        "subnet_allowed",
        "other_subnet_blocked",
        "cleanup",
    ],
    "service": [
        "create_sg",
        "apply_service_rule",
        "service_endpoint_allowed",
        "other_endpoint_blocked",
        "cleanup",
    ],
}


def main() -> int:
    """SG scoping test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Security group scoping test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument(
        "--scope",
        required=True,
        choices=["workload", "node", "subnet", "service"],
        help="Scoping level to test",
    )
    args = parser.parse_args()

    test_names = SCOPE_TESTS[args.scope]
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": f"sg_{args.scope}_scoping",
        "scope": args.scope,
        "tests": {t: {"passed": False} for t in test_names},
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's SG scoping test   ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    sg = client.create_security_group("scoping-test")             ║
    # ║    result["tests"]["create_sg"]["passed"] = True                 ║
    # ║                                                                  ║
    # ║    # Apply rule at the selected scope level                      ║
    # ║    client.add_rule(sg, scope=args.scope, target=...)             ║
    # ║    result["tests"][f"apply_{args.scope}_rule"]["passed"] = True  ║
    # ║                                                                  ║
    # ║    # Verify rule applies to target, not to others                ║
    # ║    ...                                                           ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["tests"] = {t: {"passed": True} for t in test_names}
        result["success"] = True
    else:
        result["error"] = f"Not implemented - replace with your platform's SG {args.scope}-level scoping test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
