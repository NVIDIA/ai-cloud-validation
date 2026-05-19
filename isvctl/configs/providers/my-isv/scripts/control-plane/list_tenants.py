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

"""List tenants / resource groups and verify a target exists.

Provider-agnostic template - replace the TODO section with your platform's
multi-tenancy listing calls.

Required JSON output (field names must match - TenantListedCheck reads these):
{
    "success":       bool                                - true if listing succeeded,
    "platform":      str                                 - "control_plane",
    "tenants":       list[{tenant_name, tenant_id}]      - list of tenant objects,
    "count":         int                                 - len(tenants),
    "target_tenant": str                                 - echoes --group-name,
    "found_target":  bool                                - true if target is in the list,
    "error":         str                                 - error message (present when success is false)
}

Usage:
    python list_tenants.py --region <region> --group-name my-tenant

AWS reference implementation:
    ../aws/control-plane/list_tenants.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """List tenants / resource groups and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="List tenants / resource groups")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    parser.add_argument("--group-name", required=True, help="Tenant name to look for")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenants": [],
        "count": 0,
        "target_tenant": args.group_name,
        "found_target": False,
    }

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation            ║
    # ║                                                                          ║
    # ║  1. List all tenants / resource groups / projects                        ║
    # ║     -> result["tenants"] = [{"tenant_name": "...", "tenant_id": "..."}]  ║
    # ║     -> result["count"]   = len(result["tenants"])                        ║
    # ║  2. Check if args.group_name is in the list                              ║
    # ║     -> result["found_target"] = True / False                             ║
    # ║  3. Set result["success"] = True                                         ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["tenants"] = [{"tenant_name": args.group_name, "tenant_id": "dummy-tenant-id"}]
        result["count"] = len(result["tenants"])
        result["found_target"] = True
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's tenant listing logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
