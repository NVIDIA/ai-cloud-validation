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
