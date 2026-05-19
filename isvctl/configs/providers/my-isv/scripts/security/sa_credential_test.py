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

"""Service account long-lived credential test - TEMPLATE.

Verifies that out-of-cluster service accounts can authenticate using
long-lived credentials (API keys, service account keys, etc.).  This
covers the SEC03-01 requirement.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "sa_credential_test",
    "authenticated": true,
    "credential_type": "api_key",
    "identity": "sa-validation-test@project.iam",
    "expires_at": null
  }

Usage:
    python sa_credential_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """SA credential test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Service account credential test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "sa_credential_test",
        "authenticated": False,
        "credential_type": "",
        "identity": "",
        "expires_at": None,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's SA credential     ║
    # ║  test.                                                           ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    sa = create_service_account("validation-test")                ║
    # ║    key = create_long_lived_key(sa)                               ║
    # ║    identity = authenticate_with_key(key)                         ║
    # ║    result["authenticated"] = identity is not None                ║
    # ║    result["credential_type"] = "service_account_key"             ║
    # ║    result["identity"] = identity.principal                       ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["authenticated"] = True
        result["credential_type"] = "api_key"
        result["identity"] = "sa-validation-test@my-isv.iam"
        result["expires_at"] = None
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's service account credential test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
