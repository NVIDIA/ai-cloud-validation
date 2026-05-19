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

"""DHCP/IP management test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It must:
  1. Create an SSH key pair dynamically (idempotent - reuse if exists)
  2. Launch an instance in the specified subnet
  3. Wait for SSH to become reachable
  4. Print a JSON object to stdout with SSH connection details

The DhcpIpManagementCheck validation will SSH in and verify:
  - DHCP lease is active
  - Instance IP matches platform-reported IP
  - DHCP-provided DNS options are configured

Required JSON output fields:
  {
    "success": true,                           # boolean - did the instance launch?
    "platform": "network",                     # string  - always "network"
    "test_name": "dhcp_ip",                    # string  - always "dhcp_ip"
    "public_ip": "3.1.2.3",                    # string  - SSH target address
    "private_ip": "10.0.1.5",                  # string  - expected private IP
    "key_file": "/tmp/isv-dhcp-test-key.pem",  # string  - SSH private key path
    "key_name": "isv-dhcp-test-key",           # string  - key pair name
    "ssh_user": "ubuntu",                      # string  - SSH username
    "instance_id": "i-abc123"                  # string  - instance identifier
  }

On failure, set "success": false and include an "error" field.

Usage:
    python dhcp_ip_test.py --vpc-id vpc-abc123 --subnet-id subnet-abc \\
        --sg-id sg-abc123 --region <region>

Reference implementation: ../../aws/network/dhcp_ip_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """DHCP/IP management test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="DHCP/IP management test (template)")
    parser.add_argument("--vpc-id", required=True, help="VPC / network ID")
    parser.add_argument("--subnet-id", required=True, help="Subnet to launch instance in")
    parser.add_argument("--sg-id", required=True, help="Security group ID")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "dhcp_ip",
        "public_ip": None,
        "private_ip": None,
        "key_file": None,
        "key_name": None,
        "ssh_user": args.ssh_user,
        "instance_id": None,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║                                                                  ║
    # ║    # Create key pair dynamically (idempotent)                    ║
    # ║    key_name = "isv-dhcp-test-key"                                ║
    # ║    key_file = client.create_key_pair(key_name)                   ║
    # ║    result["key_file"] = key_file                                 ║
    # ║    result["key_name"] = key_name                                 ║
    # ║                                                                  ║
    # ║    # Launch an instance in the specified subnet                  ║
    # ║    inst = client.launch_instance(                                ║
    # ║        subnet_id=args.subnet_id,                                 ║
    # ║        security_group=args.sg_id,                                ║
    # ║        key_name=key_name,                                        ║
    # ║    )                                                             ║
    # ║    result["instance_id"] = inst.id                               ║
    # ║    result["public_ip"] = inst.public_ip                          ║
    # ║    result["private_ip"] = inst.private_ip                        ║
    # ║    result["success"] = True                                      ║
    # ║                                                                  ║
    # ║  Note: The DhcpIpManagementCheck validation handles SSH and      ║
    # ║  DHCP verification. This script just needs to provide instance   ║
    # ║  connection details.                                             ║
    # ║                                                                  ║
    # ║  Cleanup: Instance should be terminated in teardown phase or     ║
    # ║  by a separate cleanup script.                                   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["instance_id"] = "dummy-dhcp-instance"
        result["public_ip"] = "203.0.113.50"
        result["private_ip"] = "10.0.1.5"
        result["key_file"] = "/tmp/dummy-dhcp-key.pem"
        result["key_name"] = "dummy-dhcp-key"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's instance launch logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
