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

"""Create a test user and generate an access key / API token.

Provider-agnostic template  - replace the TODO section with your platform's
identity management calls (e.g. Keystone, IAM, service accounts, etc.).

Required JSON output:
{
    "success":          bool  - true if user and key created,
    "platform":         str   - "control_plane",
    "username":         str   - name of the created test user,
    "access_key_id":    str   - public credential identifier,
    "secret_access_key": str  - secret credential value,
    "error":             str  - (optional) error message, present when success is false
}

Usage:
    python create_access_key.py --region <region>

AWS reference implementation:
    ../aws/control-plane/create_access_key.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Create test user and access key and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Create test user and access key")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    args = parser.parse_args()  # noqa: F841 - used in TODO block below

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "username": "",
        "access_key_id": "",
        "secret_access_key": "",
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Create a test user / service account                         ║
    # ║     -> result["username"] = "<created-username>"                 ║
    # ║  2. Generate an access key or API token for the user             ║
    # ║     -> result["access_key_id"]     = "<key-id>"                  ║
    # ║     -> result["secret_access_key"] = "<secret>"                  ║
    # ║  3. Set result["success"] = True                                 ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["username"] = "isv-test-user"
        result["access_key_id"] = "dummy-key-id"
        result["secret_access_key"] = "dummy-secret"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's access key creation logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
