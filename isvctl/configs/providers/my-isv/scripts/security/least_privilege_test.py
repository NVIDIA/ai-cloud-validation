#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Least-privilege policy test - TEMPLATE (replace with your platform implementation).

Verifies that a temporary principal receives only the minimum access needed
for one in-scope operation, and that out-of-scope compute, storage, and
network operations are denied. Covers SEC04-01 and SEC04-02.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "least_privilege_test",
    "test_identity": "<temporary principal id>",
    "allowed_resource": "<resource allowed by the minimal policy>",
    "allowed_source_cidr": "<network source constraint>",
    "tests": {
      "policy_dimensions_user_based":              {"passed": true},
      "policy_dimensions_resource_based":          {"passed": true},
      "policy_dimensions_network_based":           {"passed": true},
      "policy_dimensions_allowed_action_succeeds": {"passed": true},
      "out_of_scope_compute_denied":               {"passed": true},
      "out_of_scope_storage_denied":               {"passed": true},
      "out_of_scope_network_denied":               {"passed": true}
    }
  }

Usage:
    python least_privilege_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Least-privilege policy test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Least-privilege policy test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "least_privilege_test",
        "region": args.region,
        "test_identity": "",
        "allowed_resource": "",
        "allowed_source_cidr": "",
        "tests": {
            "policy_dimensions_user_based": {"passed": False},
            "policy_dimensions_resource_based": {"passed": False},
            "policy_dimensions_network_based": {"passed": False},
            "policy_dimensions_allowed_action_succeeds": {"passed": False},
            "out_of_scope_compute_denied": {"passed": False},
            "out_of_scope_storage_denied": {"passed": False},
            "out_of_scope_network_denied": {"passed": False},
        },
    }

    # TODO: Replace this block with your platform's SEC04 test.
    #
    # Suggested shape:
    #   principal = create_temporary_principal()
    #   resource = create_temporary_resource()
    #   attach_minimal_policy(
    #       principal,
    #       allowed_action="list/read metadata",
    #       allowed_resource=resource.id,
    #       allowed_source_cidr=current_runner_cidr,
    #   )
    #   assert principal.can_perform_allowed_action(resource)
    #   assert principal.cannot_use_compute_actions_outside_policy()
    #   assert principal.cannot_use_storage_actions_outside_policy()
    #   assert principal.cannot_use_network_actions_outside_policy()
    #   teardown(principal, resource)
    #
    # If no real policy fixture can be created in this environment, emit
    # top-level ``skipped: true`` plus ``skip_reason`` and exit 0.

    if DEMO_MODE:
        result["test_identity"] = "demo-sec04-principal"
        result["allowed_resource"] = "demo-sec04-resource"
        result["allowed_source_cidr"] = "203.0.113.10/32"
        result["tests"] = {
            "policy_dimensions_user_based": {"passed": True, "message": "demo: policy attached to one principal"},
            "policy_dimensions_resource_based": {"passed": True, "message": "demo: policy scoped to one resource"},
            "policy_dimensions_network_based": {"passed": True, "message": "demo: policy scoped to one source CIDR"},
            "policy_dimensions_allowed_action_succeeds": {"passed": True, "message": "demo: in-scope action allowed"},
            "out_of_scope_compute_denied": {"passed": True, "message": "demo: compute operations denied"},
            "out_of_scope_storage_denied": {"passed": True, "message": "demo: storage operations denied"},
            "out_of_scope_network_denied": {"passed": True, "message": "demo: network operations denied"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's least-privilege policy test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
