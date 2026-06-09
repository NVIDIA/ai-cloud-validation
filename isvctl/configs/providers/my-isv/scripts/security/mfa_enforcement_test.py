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

"""MFA enforcement test - TEMPLATE (replace with your platform implementation).

Verifies that ALL administrative interfaces (UI, CLI, API) are protected
by Multi-Factor Authentication.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "mfa_enforcement",
    "interfaces_checked": 3,
    "tests": {
      "admin_account_mfa":       {"passed": true},  # root/admin account requires MFA
      "interactive_access_mfa":  {"passed": true},  # console/UI sign-in requires MFA
      "programmatic_access_mfa": {"passed": true}   # API+CLI access is MFA-gated
    }
  }

Usage:
    python mfa_enforcement_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """MFA enforcement test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="MFA enforcement test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "mfa_enforcement",
        "interfaces_checked": 0,
        "tests": {
            "admin_account_mfa": {"passed": False},
            "interactive_access_mfa": {"passed": False},
            "programmatic_access_mfa": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's MFA enforcement   ║
    # ║  test. Emit {"passed": bool} per outcome, attested via your      ║
    # ║  platform's native MFA control:                                  ║
    # ║                                                                  ║
    # ║    1. admin_account_mfa       - root/admin account requires MFA  ║
    # ║    2. interactive_access_mfa  - console/UI sign-in requires MFA  ║
    # ║    3. programmatic_access_mfa - principal API+CLI is MFA-gated   ║
    # ║                                 (not machine/token credentials)  ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["interfaces_checked"] = 3
        result["tests"] = {
            "admin_account_mfa": {"passed": True, "message": "Admin account MFA enabled"},
            "interactive_access_mfa": {"passed": True, "message": "2/2 console users have MFA"},
            "programmatic_access_mfa": {"passed": True, "message": "MFA condition in programmatic-access policy"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's MFA enforcement test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
