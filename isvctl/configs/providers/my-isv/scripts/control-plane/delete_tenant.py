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

"""Delete a tenant / resource group / project.

Provider-agnostic template  - replace the TODO section with your platform's
tenant deletion calls.

Required JSON output:
{
    "success":           bool       - true if tenant deleted,
    "platform":          str        - "control_plane",
    "resources_deleted": list[str]  - names/IDs of deleted resources,
    "message":           str        - human-readable summary,
    "error":             str        - (optional) human-readable error details, present when success is false
}

Usage:
    python delete_tenant.py --group-name my-tenant --region <region>

AWS reference implementation:
    ../aws/control-plane/delete_tenant.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Delete tenant / resource group and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Delete tenant / resource group")
    parser.add_argument("--group-name", required=True, help="Tenant / group to delete")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "resources_deleted": [],
        "message": "",
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Delete the tenant / resource group / project                 ║
    # ║     -> result["resources_deleted"].append("tenant:<name>")       ║
    # ║  2. Set result["message"] and result["success"] = True           ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["resources_deleted"].append(f"tenant:{args.group_name}")
        result["message"] = "Tenant deleted"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's tenant deletion logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
