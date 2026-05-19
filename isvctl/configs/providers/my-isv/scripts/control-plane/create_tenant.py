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

"""Create a tenant, resource group, or project.

Provider-agnostic template - replace the TODO section with your platform's
multi-tenancy API calls (e.g. OpenStack projects, Azure resource groups,
GCP projects, etc.).

Required JSON output:
{
    "success":     bool  - true if tenant created,
    "platform":    str   - "control_plane",
    "tenant_name": str   - human-readable name of the tenant,
    "tenant_id":   str   - unique identifier for the tenant,
    "error":       str   - (optional) error message, present when success is false
}

Usage:
    python create_tenant.py --region <region>

AWS reference implementation:
    ../aws/control-plane/create_tenant.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Create tenant / resource group and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Create tenant / resource group")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    _args = parser.parse_args()  # TODO: use _args when implementing this stub

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenant_name": "",
        "tenant_id": "",
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Create a tenant / resource group / project                   ║
    # ║     -> result["tenant_name"] = "<tenant-name>"                   ║
    # ║     -> result["tenant_id"]   = "<tenant-id>"                     ║
    # ║  2. Set result["success"] = True                                 ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["tenant_name"] = "dummy-tenant"
        result["tenant_id"] = "dummy-tenant-id"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's tenant creation logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
