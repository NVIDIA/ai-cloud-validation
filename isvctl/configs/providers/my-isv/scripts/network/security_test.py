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

"""Security blocking test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create a temporary VPC
  2. Test that default security group denies all inbound traffic
  3. Add specific allow rules and verify they take effect
  4. Verify egress rules behave as expected
  5. Clean up all resources
  6. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                              # boolean - did all security tests pass?
    "platform": "network",                        # string  - always "network"
    "test_name": "security_blocking",             # string  - always "security_blocking"
    "network_id": "vpc-...",                      # string  - VPC used for the test
    "tests": {                                    # object  - per-step results
      "create_vpc":                  {"passed": true},
      "sg_default_deny_inbound":     {"passed": true}, # SG denies all inbound by default
      "sg_allows_specific_ssh":      {"passed": true}, # SG allow rule for SSH works
      "sg_denies_vpc_icmp":          {"passed": true}, # SG denies VPC-internal ICMP
      "nacl_explicit_deny":          {"passed": true}, # NACL explicit-deny rule applies
      "default_nacl_allows_inbound": {"passed": true}, # Default NACL allows inbound
      "sg_restricted_egress":        {"passed": true}  # Egress restrictions enforced
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python security_test.py --region <region> --cidr 10.94.0.0/16

Reference implementation: ../../aws/network/security_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Security blocking test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Security blocking test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", default="10.94.0.0/16", help="CIDR block for test VPC")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "security_blocking",
        "tests": {
            "create_vpc": {"passed": False},
            "sg_default_deny_inbound": {"passed": False},
            "sg_allows_specific_ssh": {"passed": False},
            "sg_denies_vpc_icmp": {"passed": False},
            "nacl_explicit_deny": {"passed": False},
            "default_nacl_allows_inbound": {"passed": False},
            "sg_restricted_egress": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's security test     ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    vpc = client.create_vpc(cidr=args.cidr)                       ║
    # ║    sg = client.create_security_group(vpc.id, "test-sg")          ║
    # ║                                                                  ║
    # ║    # Test default deny                                           ║
    # ║    rules = client.describe_sg_rules(sg.id)                       ║
    # ║    no_inbound = len(rules.inbound) == 0                          ║
    # ║    result["tests"]["sg_default_deny_inbound"]["passed"] = no_inbound ║
    # ║                                                                  ║
    # ║    # Test specific allow                                         ║
    # ║    client.authorize_ingress(sg.id, port=22, cidr="10.0.0.0/8")   ║
    # ║    rules = client.describe_sg_rules(sg.id)                       ║
    # ║    has_ssh = any(r.port == 22 for r in rules.inbound)            ║
    # ║    result["tests"]["sg_allows_specific_ssh"]["passed"] = has_ssh  ║
    # ║                                                                  ║
    # ║    # Test egress rules                                           ║
    # ║    egress = client.describe_sg_rules(sg.id).outbound             ║
    # ║    result["tests"]["sg_restricted_egress"]["passed"] = len(egress) > 0 ║
    # ║                                                                  ║
    # ║    # Cleanup                                                     ║
    # ║    client.delete_vpc(vpc.id, cascade=True)                       ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["network_id"] = "dummy-vpc-sec"
        result["tests"] = {
            "create_vpc": {"passed": True},
            "sg_default_deny_inbound": {"passed": True},
            "sg_allows_specific_ssh": {"passed": True},
            "sg_denies_vpc_icmp": {"passed": True},
            "nacl_explicit_deny": {"passed": True},
            "default_nacl_allows_inbound": {"passed": True},
            "sg_restricted_egress": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's security test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
