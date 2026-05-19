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

"""Security Group CRUD lifecycle test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create a temporary VPC
  2. Create a security group in that VPC
  3. Read / list the security group and verify attributes
  4. Update the security group (add/modify/remove rules)
  5. Delete the security group
  6. Verify deletion (group no longer exists)
  7. Clean up the VPC
  8. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                           # boolean - did all operations pass?
    "platform": "network",                     # string  - always "network"
    "test_name": "sg_crud",                    # string  - always "sg_crud"
    "tests": {                                 # object  - per-operation results
      "create_vpc": {
        "passed": true                         # boolean - VPC created?
      },
      "create_sg": {
        "passed": true,                        # boolean - SG created?
        "sg_id": "sg-abc123"                   # string  - created SG ID
      },
      "read_sg": {
        "passed": true,                        # boolean - SG readable?
        "name": "...",                         # string  - SG name
        "description": "..."                   # string  - SG description
      },
      "update_sg_add_rule": {
        "passed": true                         # boolean - rule added?
      },
      "update_sg_modify_rule": {
        "passed": true                         # boolean - rule modified?
      },
      "update_sg_remove_rule": {
        "passed": true                         # boolean - rule removed?
      },
      "delete_sg": {
        "passed": true                         # boolean - SG deleted?
      },
      "verify_deleted": {
        "passed": true                         # boolean - SG gone?
      }
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python sg_crud_test.py --region <region> --cidr 10.95.0.0/16

Reference implementation: ../../aws/network/sg_crud_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run Security Group CRUD lifecycle test (template - not implemented).

    Parses --region and --cidr CLI args and prints a JSON result indicating
    the test is not yet implemented. Replace the TODO block with your
    platform's SG CRUD logic.

    Returns:
        0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(description="Security Group CRUD lifecycle test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", default="10.95.0.0/16", help="CIDR block for test VPC")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sg_crud",
        "tests": {
            "create_vpc": {"passed": False},
            "create_sg": {"passed": False},
            "read_sg": {"passed": False},
            "update_sg_add_rule": {"passed": False},
            "update_sg_modify_rule": {"passed": False},
            "update_sg_remove_rule": {"passed": False},
            "delete_sg": {"passed": False},
            "verify_deleted": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's SG CRUD test      ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    vpc = client.create_vpc(cidr=args.cidr)                       ║
    # ║                                                                  ║
    # ║    # CREATE                                                      ║
    # ║    sg = client.create_security_group(vpc.id, "test-sg")          ║
    # ║    result["tests"]["create_sg"]["passed"] = True                 ║
    # ║    result["tests"]["create_sg"]["sg_id"] = sg.id                 ║
    # ║                                                                  ║
    # ║    # READ                                                        ║
    # ║    info = client.describe_security_group(sg.id)                  ║
    # ║    result["tests"]["read_sg"]["passed"] = True                   ║
    # ║    result["tests"]["read_sg"]["name"] = info.name                ║
    # ║                                                                  ║
    # ║    # UPDATE - add rule                                           ║
    # ║    client.authorize_ingress(sg.id, port=443, cidr="0.0.0.0/0")   ║
    # ║    result["tests"]["update_sg_add_rule"]["passed"] = True        ║
    # ║                                                                  ║
    # ║    # UPDATE - modify (replace with different port)               ║
    # ║    client.revoke_ingress(sg.id, port=443, cidr="0.0.0.0/0")      ║
    # ║    client.authorize_ingress(sg.id, port=8443, cidr="0.0.0.0/0")  ║
    # ║    result["tests"]["update_sg_modify_rule"]["passed"] = True     ║
    # ║                                                                  ║
    # ║    # UPDATE - remove rule                                        ║
    # ║    client.revoke_ingress(sg.id, port=8443, cidr="0.0.0.0/0")     ║
    # ║    rules = client.describe_sg_rules(sg.id)                       ║
    # ║    assert len(rules.inbound) == 0                                ║
    # ║    result["tests"]["update_sg_remove_rule"]["passed"] = True     ║
    # ║                                                                  ║
    # ║    # DELETE                                                      ║
    # ║    client.delete_security_group(sg.id)                           ║
    # ║    result["tests"]["delete_sg"]["passed"] = True                 ║
    # ║                                                                  ║
    # ║    # VERIFY DELETED                                              ║
    # ║    assert client.get_sg(sg.id) raises NotFound                   ║
    # ║    result["tests"]["verify_deleted"]["passed"] = True            ║
    # ║    result["success"] = True                                      ║
    # ║                                                                  ║
    # ║    # Cleanup                                                     ║
    # ║    client.delete_vpc(vpc.id)                                     ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["network_id"] = "dummy-vpc-sg"
        result["tests"] = {
            "create_vpc": {"passed": True},
            "create_sg": {"passed": True, "sg_id": "dummy-sg-crud"},
            "read_sg": {"passed": True, "name": "isv-demo-sg", "description": "ISV demo security group"},
            "update_sg_add_rule": {"passed": True},
            "update_sg_modify_rule": {"passed": True},
            "update_sg_remove_rule": {"passed": True},
            "delete_sg": {"passed": True},
            "verify_deleted": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's SG CRUD logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
