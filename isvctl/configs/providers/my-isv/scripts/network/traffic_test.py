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

"""Traffic flow test - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Create a temporary VPC with subnets and security groups
  2. Launch instances in different subnets
  3. Test that ping is ALLOWED between instances in the same security group
  4. Test that ping is BLOCKED when security group rules deny ICMP
  5. Test internet access (e.g., via NAT gateway or internet gateway)
  6. Clean up all resources
  7. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,                            # boolean - did all traffic tests pass?
    "platform": "network",                      # string  - always "network"
    "test_name": "traffic_flow",                # string  - always "traffic_flow"
    "network_id": "vpc-...",                    # string  - VPC used for the test
    "tests": {                                  # object  - per-step results
      "create_vpc":             {"passed": true},
      "create_igw":             {"passed": true},
      "network_setup":          {"passed": true},
      "create_iam":             {"passed": true},
      "create_security_groups": {"passed": true},
      "launch_instances":       {"passed": true},
      "instances_running":      {"passed": true},
      "ssm_ready":              {"passed": true},
      "traffic_allowed":        {"passed": true,
                                 "latency_ms": 0.5}, # optional latency
      "traffic_blocked":        {"passed": true},
      "internet_icmp":          {"passed": true},
      "internet_http":          {"passed": true}
    }
  }

On failure, set "success": false and include an "error" field.

Usage:
    python traffic_test.py --region <region> --cidr 10.93.0.0/16

Reference implementation: ../../aws/network/traffic_test.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Traffic flow test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Traffic flow test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", default="10.93.0.0/16", help="CIDR block for test VPC")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "traffic_flow",
        "network_id": "",
        "tests": {
            "create_vpc": {"passed": False},
            "create_igw": {"passed": False},
            "network_setup": {"passed": False},
            "create_iam": {"passed": False},
            "create_security_groups": {"passed": False},
            "launch_instances": {"passed": False},
            "instances_running": {"passed": False},
            "ssm_ready": {"passed": False},
            "traffic_allowed": {"passed": False},
            "traffic_blocked": {"passed": False},
            "internet_icmp": {"passed": False},
            "internet_http": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's traffic test      ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    vpc = client.create_vpc(cidr=args.cidr)                       ║
    # ║    subnet = client.create_subnet(vpc.id, "10.93.1.0/24")         ║
    # ║    sg = client.create_security_group(vpc.id, "traffic-test")     ║
    # ║    client.authorize_ingress(sg.id, protocol="icmp", cidr="*")    ║
    # ║                                                                  ║
    # ║    inst_a = client.launch_instance(subnet.id, sg.id)             ║
    # ║    inst_b = client.launch_instance(subnet.id, sg.id)             ║
    # ║                                                                  ║
    # ║    # Test ping allowed                                           ║
    # ║    ok = client.ping(inst_a.id, inst_b.private_ip)                ║
    # ║    result["tests"]["traffic_allowed"]["passed"] = ok             ║
    # ║                                                                  ║
    # ║    # Test ping blocked (revoke ICMP rule)                        ║
    # ║    client.revoke_ingress(sg.id, protocol="icmp")                 ║
    # ║    blocked = not client.ping(inst_a.id, inst_b.private_ip)       ║
    # ║    result["tests"]["traffic_blocked"]["passed"] = blocked        ║
    # ║                                                                  ║
    # ║    # Test internet access                                        ║
    # ║    igw = client.create_internet_gateway(vpc.id)                  ║
    # ║    inet = client.ping(inst_a.id, "8.8.8.8")                      ║
    # ║    result["tests"]["internet_icmp"]["passed"] = inet             ║
    # ║                                                                  ║
    # ║    # Cleanup                                                     ║
    # ║    client.terminate_instances([inst_a.id, inst_b.id])            ║
    # ║    client.delete_vpc(vpc.id, cascade=True)                       ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["network_id"] = "dummy-vpc-traffic"
        result["tests"] = {
            "create_vpc": {"passed": True},
            "create_igw": {"passed": True},
            "network_setup": {"passed": True},
            "create_iam": {"passed": True},
            "create_security_groups": {"passed": True},
            "launch_instances": {"passed": True},
            "instances_running": {"passed": True},
            "ssm_ready": {"passed": True},
            "traffic_allowed": {"passed": True, "latency_ms": 0.5},
            "traffic_blocked": {"passed": True},
            "internet_icmp": {"passed": True},
            "internet_http": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's traffic test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
