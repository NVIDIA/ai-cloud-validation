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

"""Console RBAC validation - TEMPLATE (replace with your platform implementation).

This script must prove interactive console access is controlled by RBAC for
the target VM, not merely that serial console output exists.

Required JSON output fields:
  {
    "success": true,
    "platform": "vm",
    "test_name": "console_rbac",
    "instance_id": "<id>",
    "rbac_model": "<provider-rbac-model>",
    "access_restricted": true,
    "restricted_actions": ["console:Connect"],
    "tests": {
      "denied_principal_cannot_access_console": {"passed": true},
      "allowed_principal_can_access_console": {"passed": true},
      "allowed_principal_is_resource_scoped": {"passed": true}
    }
  }

Usage:
    python console_rbac.py --instance-id <id> --region <region>

Reference implementation: ../../../aws/scripts/vm/console_rbac.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _demo_result(instance_id: str) -> dict[str, Any]:
    """Return a passing demo payload for the console RBAC contract."""
    return {
        "success": True,
        "platform": "vm",
        "test_name": "console_rbac",
        "instance_id": instance_id,
        "rbac_model": "my-isv-rbac",
        "access_restricted": True,
        "restricted_actions": ["console:Connect"],
        "tests": {
            "denied_principal_cannot_access_console": {
                "passed": True,
                "principal": "console-rbac-denied-demo",
            },
            "allowed_principal_can_access_console": {
                "passed": True,
                "principal": "console-rbac-allowed-demo",
            },
            "allowed_principal_is_resource_scoped": {
                "passed": True,
                "principal": "console-rbac-allowed-demo",
            },
        },
    }


def main() -> int:
    """Validate console RBAC and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Console RBAC validation (template)")
    parser.add_argument("--instance-id", required=True, help="Instance ID")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's console RBAC      ║
    # ║  validation.                                                     ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    denied = create_principal_without_console_access()            ║
    # ║    denied_error = attempt_console_access(denied, args.instance_id)║
    # ║    allowed = create_principal_with_console_access(args.instance_id)║
    # ║    allowed_session = attempt_console_access(allowed, instance_id) ║
    # ║    other_denied = attempt_console_access(allowed, other_vm_id)    ║
    # ║                                                                  ║
    # ║  The result must prove:                                          ║
    # ║    1. A principal without console rights is denied.              ║
    # ║    2. A principal with scoped console rights is allowed.         ║
    # ║    3. The allowed principal is denied for a different VM.        ║
    # ║                                                                  ║
    # ║  Populate all required `tests` entries with {"passed": true}.    ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result = _demo_result(args.instance_id)
    else:
        result = {
            "success": False,
            "platform": "vm",
            "test_name": "console_rbac",
            "instance_id": args.instance_id,
            "rbac_model": "",
            "access_restricted": False,
            "restricted_actions": [],
            "tests": {
                "denied_principal_cannot_access_console": {"passed": False},
                "allowed_principal_can_access_console": {"passed": False},
                "allowed_principal_is_resource_scoped": {"passed": False},
            },
            "error": "Not implemented - replace with your platform's console RBAC validation",
        }

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
