#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Port security policy test - TEMPLATE (replace with your platform implementation).

Tests that a custom ingress port policy can be applied to one virtual
interface without allowing adjacent/unlisted ports or affecting another
virtual interface.

Required JSON output:
  tests: {create_virtual_interface, apply_port_policy,
          allowed_port_permitted, unlisted_port_blocked,
          other_interface_unaffected, cleanup}

Usage:
    python sg_port_security_policy.py --region <region> --allowed-port 8443
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

TEST_NAMES = [
    "create_virtual_interface",
    "apply_port_policy",
    "allowed_port_permitted",
    "unlisted_port_blocked",
    "other_interface_unaffected",
    "cleanup",
]


def main() -> int:
    """Run the port security policy template probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Port security policy test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--allowed-port", type=int, default=8443, help="TCP port to allow on the target interface")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sg_port_security_policy",
        "tests": {name: {"passed": False} for name in TEST_NAMES},
    }

    # TODO: Replace this block with your platform's virtual-interface port
    # security policy implementation.
    if DEMO_MODE:
        result["tests"] = {name: {"passed": True} for name in TEST_NAMES}
        result["tests"]["allowed_port_permitted"]["message"] = f"TCP/{args.allowed_port} is allowed"
        result["tests"]["unlisted_port_blocked"]["message"] = f"TCP/{args.allowed_port + 1} is not allowed"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's virtual-interface port policy test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
