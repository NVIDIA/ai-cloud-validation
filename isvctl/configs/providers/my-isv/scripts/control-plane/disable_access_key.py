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

"""Disable / deactivate an access key.

Provider-agnostic template - replace the TODO section with your platform's
credential management calls (e.g. set key status to inactive, revoke token).

Required JSON output:
{
    "success":       bool  - true if key was disabled,
    "platform":      str   - "control_plane",
    "access_key_id": str   - the key that was disabled,
    "status":        str   - "Inactive" (must match exactly; AccessKeyDisabledCheck compares case-sensitively),
    "error":         str or null - error message when disabling fails; null/absent on success
}

Usage:
    python disable_access_key.py --username testuser --access-key-id AKID --region <region>

AWS reference implementation:
    ../aws/control-plane/disable_access_key.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Disable an access key and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Disable an access key")
    parser.add_argument("--username", required=True, help="User who owns the key")
    parser.add_argument("--access-key-id", required=True, help="Key to disable")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "access_key_id": args.access_key_id,
        "status": "",
        "error": None,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Disable / deactivate the access key                          ║
    # ║     (e.g. set status to Inactive, revoke the token)              ║
    # ║  2. On success:                                                  ║
    # ║     -> result["status"]  = "Inactive"                            ║
    # ║     -> result["success"] = True                                  ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["status"] = "Inactive"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's key disable logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
