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

"""BMC protocol security test - TEMPLATE (replace with your platform implementation).

Verifies CNP10-01 management protocol posture for BMC endpoints:

* IPMI is disabled.
* Redfish is served only over TLS.
* Plain HTTP Redfish is disabled.
* Unauthenticated Redfish requests are rejected.
* Authenticated Redfish users are authorized according to role.
* Redfish AAA/accounting evidence is present.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_protocol_security",
    "bmc_endpoints_tested": 1,
    "tests": {
      "ipmi_disabled": {"passed": true},
      "redfish_tls_enabled": {"passed": true},
      "redfish_plain_http_disabled": {"passed": true},
      "redfish_authentication_required": {"passed": true},
      "redfish_authorization_enforced": {"passed": true},
      "redfish_accounting_enabled": {"passed": true}
    }
  }

Implementation guidance:
  Discover the BMC endpoints for the tenant or bare-metal host under test.
  For each endpoint, probe UDP 623/IPMI and assert it is closed or refused.
  Probe Redfish over HTTPS and validate TLS protocol/certificate posture.
  Probe Redfish over plain HTTP and assert it is refused or redirects only to
  HTTPS. Send an unauthenticated Redfish request and assert HTTP 401/403.
  Authenticate as a low-privilege Redfish user and assert privileged actions
  are rejected. Finally, retrieve BMC audit logs or provider control-plane
  audit records proving Redfish activity is accounted.

Usage:
    python bmc_protocol_security_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

REQUIRED_TESTS = [
    "ipmi_disabled",
    "redfish_tls_enabled",
    "redfish_plain_http_disabled",
    "redfish_authentication_required",
    "redfish_authorization_enforced",
    "redfish_accounting_enabled",
]


def main() -> int:
    """Run the BMC protocol security template and emit structured JSON."""
    parser = argparse.ArgumentParser(description="BMC protocol security test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_protocol_security",
        "bmc_endpoints_tested": 0,
        "tests": {name: {"passed": False} for name in REQUIRED_TESTS},
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's BMC protocol      ║
    # ║  security probes.                                                ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    endpoints = get_bmc_endpoints(region=args.region)             ║
    # ║    for endpoint in endpoints:                                    ║
    # ║        assert not udp_probe(endpoint.host, port=623)  # IPMI     ║
    # ║        assert redfish_tls_valid(endpoint.https_url)              ║
    # ║        assert not redfish_plain_http_allowed(endpoint.http_url)  ║
    # ║        assert redfish_rejects_unauthenticated(endpoint)          ║
    # ║        assert redfish_rejects_low_privilege_actions(endpoint)    ║
    # ║    assert redfish_accounting_evidence_exists(endpoints)          ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["bmc_endpoints_tested"] = 1
        result["tests"] = {
            "ipmi_disabled": {"passed": True, "message": "Demo: IPMI disabled"},
            "redfish_tls_enabled": {"passed": True, "message": "Demo: Redfish HTTPS/TLS enabled"},
            "redfish_plain_http_disabled": {"passed": True, "message": "Demo: plain HTTP rejected"},
            "redfish_authentication_required": {"passed": True, "message": "Demo: unauthenticated Redfish rejected"},
            "redfish_authorization_enforced": {"passed": True, "message": "Demo: role authorization enforced"},
            "redfish_accounting_enabled": {"passed": True, "message": "Demo: Redfish audit evidence present"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's BMC protocol security test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
