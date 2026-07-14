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

"""Component key access (SOL / network devices) - TEMPLATE.

AUTH03-01: prove the specified key from launch can access serial-over-LAN
and network devices where the platform exposes them.

Required JSON output fields:
  {
    "success": true,
    "platform": "vm",
    "test_name": "component_key_access",
    "instance_id": "<id>",
    "key_name": "<key>",
    "tests": {
      "sol_access": {"passed": true},
      "network_device_access": {"passed": true}
    }
  }

When a component class is not customer-visible, mark that subtest
``provider_hidden: true`` with ``passed: true`` instead of failing.

Usage:
    python component_key_access.py --instance-id <id> --key-file <path> \\
        --key-name <name> --region <region>

Reference implementation: ../../../aws/scripts/vm/component_key_access.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _demo_result(instance_id: str, key_name: str) -> dict[str, Any]:
    """Return a passing demo payload for the AUTH03 contract."""
    return {
        "success": True,
        "platform": "vm",
        "test_name": "component_key_access",
        "instance_id": instance_id,
        "key_name": key_name,
        "tests": {
            "sol_access": {
                "passed": True,
                "message": "Demo SOL access via specified key",
                "probes": ["serial_console_ssh"],
            },
            "network_device_access": {
                "passed": True,
                "message": "Demo network-device access via specified key",
                "probes": ["network_device_ssh"],
            },
        },
    }


def main() -> int:
    """Probe key-based SOL / network-device access (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="Component key access (template)")
    parser.add_argument("--instance-id", required=True, help="Instance identifier")
    parser.add_argument("--key-file", required=True, help="Path to the instance private key")
    parser.add_argument("--key-name", required=True, help="Key name requested at launch")
    parser.add_argument("--region", default="my-isv-region-1", help="Region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "test_name": "component_key_access",
        "instance_id": args.instance_id,
        "key_name": args.key_name,
        "tests": {},
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's key-access proof  ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    sol = platform.authorize_sol_key(                             ║
    # ║        instance_id=args.instance_id,                             ║
    # ║        key_file=args.key_file,                                   ║
    # ║    )                                                             ║
    # ║    result["tests"]["sol_access"] = {"passed": sol.ok}            ║
    # ║    net = platform.probe_network_device_key(args.key_file)        ║
    # ║    result["tests"]["network_device_access"] = {                   ║
    # ║        "passed": True,                                           ║
    # ║        "provider_hidden": not net.available,                     ║
    # ║    }                                                             ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result = _demo_result(args.instance_id, args.key_name)
    else:
        result["error"] = "Not implemented - replace with your platform's component key access logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
