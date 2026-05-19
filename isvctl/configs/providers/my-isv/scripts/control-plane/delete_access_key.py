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

"""Delete an access key and its associated test user.

Provider-agnostic template - replace the TODO section with your platform's
credential and user cleanup calls.

Required JSON output:
{
    "success":           bool       - true if cleanup succeeded,
    "platform":          str        - "control_plane",
    "resources_deleted": list[str]  - names/IDs of deleted resources,
    "message":           str        - human-readable summary,
    "error":             str        - (optional) error message, present when success is false
}

Usage:
    python delete_access_key.py --username testuser --access-key-id AKID --region <region>

AWS reference implementation:
    ../aws/control-plane/delete_access_key.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Delete access key and test user and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Delete access key and test user")
    parser.add_argument("--username", required=True, help="User who owns the key")
    parser.add_argument("--access-key-id", required=True, help="Key to delete")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "resources_deleted": [],
        "message": "",
    }

    # ╔════════════════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation              ║
    # ║                                                                            ║
    # ║  Available arguments:                                                      ║
    # ║    args.username        - user who owns the key                            ║
    # ║    args.access_key_id   - key to delete                                    ║
    # ║    args.region          - cloud region                                     ║
    # ║                                                                            ║
    # ║  1. Delete the access key / API token                                      ║
    # ║     -> result["resources_deleted"].append(f"access_key:{...access_key_id}")║
    # ║  2. Delete the test user / service account                                 ║
    # ║     -> result["resources_deleted"].append(f"user:{args.username}")         ║
    # ║  3. Set result["message"] and result["success"] = True                     ║
    # ╚════════════════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["resources_deleted"].append(f"access_key:{args.access_key_id}")
        result["resources_deleted"].append(f"user:{args.username}")
        result["message"] = "Access key and user deleted"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's access key deletion logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
