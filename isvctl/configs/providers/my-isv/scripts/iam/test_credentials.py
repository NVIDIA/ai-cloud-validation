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

"""Test user credentials - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It must:
  1. Use the credentials from create_user to authenticate
  2. Verify the identity / make a test API call
  3. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,           # boolean - did the overall test pass?
    "platform": "iam",         # string  - always "iam"
    "account_id": "...",       # string  - account/tenant identifier
    "tests": {                 # object  - individual test results
      "identity": {"passed": true},
      "access":   {"passed": true, "note": "optional detail"}
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python test_credentials.py --username test-user \\
        --credential-id AKID... --credential-secret SECRET...

Reference implementation: ../../aws/iam/test_credentials.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Test IAM credentials (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Test IAM credentials (template)")
    parser.add_argument("--username", required=True, help="Username to test")
    parser.add_argument("--credential-id", required=True, help="Credential / API key ID")
    parser.add_argument("--credential-secret", required=True, help="Credential secret")
    args = parser.parse_args()  # noqa: F841

    result: dict[str, Any] = {
        "success": False,
        "platform": "iam",
        "account_id": "",
        "tests": {},
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's auth test         ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    session = MyIamClient(                                        ║
    # ║        key_id=args.credential_id,                                ║
    # ║        secret=args.credential_secret,                            ║
    # ║    )                                                             ║
    # ║    identity = session.whoami()                                   ║
    # ║    result["account_id"] = identity.account_id                    ║
    # ║    result["tests"]["identity"] = {"passed": True}                ║
    # ║    result["tests"]["access"] = {"passed": True}                  ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        # Demo-success output for the validation-contract demo test.
        # Safe to delete once the TODO above is filled in for real.
        result["account_id"] = "dummy-account-123"
        result["tests"]["identity"] = {"passed": True}
        result["tests"]["access"] = {"passed": True}
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's credential test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
