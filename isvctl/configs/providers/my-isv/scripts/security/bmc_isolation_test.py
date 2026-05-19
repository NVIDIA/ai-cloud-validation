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

"""BMC tenant isolation test - TEMPLATE (replace with your platform implementation).

Verifies that BMC/IPMI/Redfish management interfaces are NOT reachable
from tenant networks.  The test probes known BMC endpoints from a tenant
network vantage point - all probes must fail (connection refused / timeout)
for the test to pass.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_tenant_isolation",
    "bmc_endpoints_tested": 4,
    "tests": {
      "probe_bmc_from_tenant":  {"passed": true},  # generic BMC endpoint unreachable
      "probe_ipmi_port":        {"passed": true},  # UDP 623 unreachable
      "probe_redfish_port":     {"passed": true},  # TCP 443 (Redfish) unreachable
      "reverse_path_check":     {"passed": true}   # BMC cannot reach tenant network
    }
  }

Usage:
    python bmc_isolation_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """BMC isolation test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="BMC tenant isolation test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_tenant_isolation",
        "bmc_endpoints_tested": 0,
        "tests": {
            "probe_bmc_from_tenant": {"passed": False},
            "probe_ipmi_port": {"passed": False},
            "probe_redfish_port": {"passed": False},
            "reverse_path_check": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's BMC isolation     ║
    # ║  test.                                                           ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    bmc_ips = get_bmc_addresses(region=args.region)               ║
    # ║    for ip in bmc_ips:                                            ║
    # ║        assert not can_reach(ip, port=623)   # IPMI               ║
    # ║        assert not can_reach(ip, port=443)   # Redfish            ║
    # ║    # Reverse: from BMC network, try tenant subnet                ║
    # ║    assert not bmc_can_reach_tenant()                             ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["bmc_endpoints_tested"] = 4
        result["tests"] = {
            "probe_bmc_from_tenant": {"passed": True},
            "probe_ipmi_port": {"passed": True},
            "probe_redfish_port": {"passed": True},
            "reverse_path_check": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's BMC isolation test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
