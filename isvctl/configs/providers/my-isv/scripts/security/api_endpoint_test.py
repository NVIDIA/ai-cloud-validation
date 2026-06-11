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

"""API endpoint isolation test - TEMPLATE (replace with your platform implementation).

Verifies that platform API endpoints (control plane, management APIs) are
NOT accessible from the public internet by default.  Probes from a public
vantage point must be refused or time out.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "api_endpoint_isolation",
    "endpoints_tested": 4,
    "tests": {
      "probe_api_from_public":   {"passed": true},  # API not reachable from internet
      "probe_mgmt_from_public":  {"passed": true},  # management UI not reachable
      "verify_private_only":     {"passed": true},  # endpoint resolves to private IP
      "dns_not_public":          {"passed": true}   # DNS record is not in public zone
    }
  }

Usage:
    python api_endpoint_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """API endpoint isolation test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="API endpoint isolation test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "api_endpoint_isolation",
        "endpoints_tested": 0,
        "tests": {
            "probe_api_from_public": {"passed": False},
            "probe_mgmt_from_public": {"passed": False},
            "verify_private_only": {"passed": False},
            "dns_not_public": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's API endpoint      ║
    # ║  isolation test.                                                 ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    endpoints = get_api_endpoints(region=args.region)             ║
    # ║    for ep in endpoints:                                          ║
    # ║        # From public internet, try to connect                    ║
    # ║        assert not can_reach_from_public(ep.url)                  ║
    # ║        # Verify endpoint resolves to private IP                  ║
    # ║        ip = resolve(ep.hostname)                                 ║
    # ║        assert is_private_ip(ip)                                  ║
    # ║        # Verify no public DNS record                             ║
    # ║        assert not has_public_dns(ep.hostname)                    ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["endpoints_tested"] = 4
        result["tests"] = {
            "probe_api_from_public": {"passed": True},
            "probe_mgmt_from_public": {"passed": True},
            "verify_private_only": {"passed": True},
            "dns_not_public": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's API endpoint isolation test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
