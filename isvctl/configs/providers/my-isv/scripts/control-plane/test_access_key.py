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

"""Test that a previously created access key can authenticate.

Provider-agnostic template - replace the TODO section with your platform's
credential verification calls.

Required JSON output:
{
    "success":       bool  - true if authentication succeeded,
    "platform":      str   - "control_plane",
    "authenticated": bool  - true if the key successfully authenticated,
    "account_id":    str   - identity / account returned by the API,
    "error":         str   - optional error message when authentication fails (omitted on success)
}

Usage:
    python test_access_key.py --access-key-id AKID --secret-access-key SECRET --region <region>

AWS reference implementation:
    ../aws/control-plane/test_access_key.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Test access key authentication and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Test access key authentication")
    parser.add_argument("--access-key-id", required=True, help="Public credential identifier")
    parser.add_argument("--secret-access-key", required=True, help="Secret credential value")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "authenticated": False,
        "account_id": "",
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Create an API client using the provided credentials          ║
    # ║     (args.access_key_id, args.secret_access_key)                 ║
    # ║  2. Call a "get identity" / "whoami" endpoint                    ║
    # ║  3. On success:                                                  ║
    # ║     -> result["authenticated"] = True                            ║
    # ║     -> result["account_id"]    = "<returned-account-id>"         ║
    # ║     -> result["success"]       = True                            ║
    # ║  4. On failure:                                                  ║
    # ║     -> result["authenticated"] = False                           ║
    # ║     -> result["error"]         = "<error-message>"               ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["authenticated"] = True
        result["account_id"] = "dummy-account-123"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's credential verification logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
