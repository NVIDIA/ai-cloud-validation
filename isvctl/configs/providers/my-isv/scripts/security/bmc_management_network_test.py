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

"""BMC management-network test - TEMPLATE (replace with your implementation).

Verifies that BMC/IPMI/Redfish management is on a dedicated network that is
not shared with tenant traffic and is protected by restricted routes and ACLs.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_management_network",
    "management_networks_checked": 1,
    "tests": {
      "dedicated_management_network": {"passed": true},
      "restricted_management_routes": {"passed": true},
      "tenant_network_not_management": {"passed": true},
      "management_acl_enforced": {"passed": true}
    }
  }

Usage:
    python bmc_management_network_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the BMC management-network template and emit structured JSON."""
    parser = argparse.ArgumentParser(description="BMC management-network test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_management_network",
        "management_networks_checked": 0,
        "tests": {
            "dedicated_management_network": {"passed": False},
            "restricted_management_routes": {"passed": False},
            "tenant_network_not_management": {"passed": False},
            "management_acl_enforced": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's SEC12-01 BMC      ║
    # ║  management-network validation.                                  ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    mgmt_networks = list_bmc_management_networks(region)          ║
    # ║    assert all(net.dedicated for net in mgmt_networks)            ║
    # ║    assert no_routes_from_tenant_networks(mgmt_networks)          ║
    # ║    assert tenant_networks_are_not_mgmt_networks()                ║
    # ║    assert management_acls_allow_only_hardened_admin_paths()      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["management_networks_checked"] = 1
        result["tests"] = {
            "dedicated_management_network": {"passed": True},
            "restricted_management_routes": {"passed": True},
            "tenant_network_not_management": {"passed": True},
            "management_acl_enforced": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's BMC management-network test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
