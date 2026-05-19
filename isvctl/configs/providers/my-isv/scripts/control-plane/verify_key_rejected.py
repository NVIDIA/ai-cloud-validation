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

"""Verify that a disabled access key is rejected on authentication.

Provider-agnostic template - replace the TODO section with your platform's
credential verification calls. This script should EXPECT authentication to
fail; success means the key was properly rejected.

Required JSON output (field names must match - AccessKeyRejectedCheck reads these):
{
    "success":    bool  - true if the disabled key was correctly rejected,
    "platform":   str   - "control_plane",
    "rejected":   bool  - true if authentication was denied,
    "error_code": str   - category of rejection (e.g. "InvalidClientTokenId"),
    "error":      str   - (optional) error message, present when success is false
}

Usage:
    python verify_key_rejected.py --access-key-id AKID --secret-access-key SECRET --region <region>

AWS reference implementation:
    ../aws/control-plane/verify_key_rejected.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Verify disabled key is rejected and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Verify disabled key is rejected")
    parser.add_argument("--access-key-id", required=True, help="Disabled key to test")
    parser.add_argument("--secret-access-key", required=True, help="Secret for the disabled key")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "rejected": False,
        "error_code": "",
        "error": None,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Attempt to authenticate using the disabled credentials       ║
    # ║     (args.access_key_id, args.secret_access_key)                 ║
    # ║  2. If authentication FAILS (expected):                          ║
    # ║     -> result["rejected"]   = True                               ║
    # ║     -> result["error_code"] = "<rejection-error-code>"           ║
    # ║     -> result["success"]    = True                               ║
    # ║  3. If authentication SUCCEEDS (unexpected - key not disabled):  ║
    # ║     -> result["rejected"]   = False                              ║
    # ║     -> result["error"]      = "Key was not rejected"             ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["rejected"] = True
        result["error_code"] = "DummyTokenRejected"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's key rejection verification logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
