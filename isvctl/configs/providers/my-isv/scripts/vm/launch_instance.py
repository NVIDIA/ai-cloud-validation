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

"""Launch a GPU virtual machine instance.

Template stub for ISV NCP Validation. Replace the TODO section with your
platform's API calls to provision a GPU-enabled VM instance.

This script must:
  1. Create an SSH key pair (or use an existing one)
  2. Create a security group allowing SSH (port 22) inbound
  3. Launch a GPU instance with the specified type
  4. Wait until the instance is in "running" state
  5. Retrieve the public IP address

Required JSON output fields:
  success           (bool)   - whether the operation succeeded
  platform          (str)    - always "vm"
  instance_id       (str)    - unique identifier for the instance
  public_ip         (str)    - public IP address of the instance
  key_file          (str)    - path to the SSH private key file
  vpc_id            (str)    - network/VPC identifier
  state             (str)    - must be "running" on success (read by InstanceStateCheck)
  security_group_id (str)    - security group/firewall rule identifier
  requested_key_name (str)   - key pair name requested for launch
  key_name          (str)    - name of the SSH key pair
  error             (str, optional) - error message provided when success is false

Usage:
    python launch_instance.py --name isv-test-gpu --instance-type <type> --region <region>

Reference implementation (AWS):
    ../aws/vm/launch_instance.py
"""

import argparse
import json
import os
import sys

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Launch GPU VM instance and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Launch GPU VM instance")
    parser.add_argument("--name", default="isv-test-gpu", help="Instance name tag")
    parser.add_argument("--instance-type", required=True, help="GPU instance type")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "vm",
        "instance_id": "",
        "public_ip": "",
        "key_file": "",
        "vpc_id": "",
        "state": "",
        "security_group_id": "",
        "requested_key_name": "",
        "key_name": "",
    }

    try:
        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's API calls     ║
        # ║                                                              ║
        # ║  1. Create or import an SSH key pair                         ║
        # ║     key_name, key_file = create_key_pair(args.name)          ║
        # ║                                                              ║
        # ║  2. Create a security group allowing SSH (port 22)           ║
        # ║     sg_id = create_security_group(vpc_id, args.name)         ║
        # ║                                                              ║
        # ║  3. Launch a GPU instance                                    ║
        # ║     instance_id = launch_instance(                           ║
        # ║         name=args.name,                                      ║
        # ║         instance_type=args.instance_type,                    ║
        # ║         region=args.region,                                  ║
        # ║         key_name=key_name,                                   ║
        # ║         security_group_id=sg_id,                             ║
        # ║     )                                                        ║
        # ║                                                              ║
        # ║  4. Wait for the instance to reach "running" state           ║
        # ║     wait_for_running(instance_id)                            ║
        # ║                                                              ║
        # ║  5. Retrieve the public IP address                           ║
        # ║     public_ip = get_public_ip(instance_id)                   ║
        # ║                                                              ║
        # ║  6. Populate the result dict:                                ║
        # ║     result["instance_id"] = instance_id                      ║
        # ║     result["public_ip"] = public_ip                          ║
        # ║     result["key_file"] = key_file                            ║
        # ║     result["vpc_id"] = vpc_id                                ║
        # ║     result["state"] = "running"                              ║
        # ║     result["security_group_id"] = sg_id                      ║
        # ║     result["requested_key_name"] = key_name                  ║
        # ║     result["key_name"] = key_name                            ║
        # ║     result["tests"]["specified_key"] = {                     ║
        # ║         "passed": key_name == observed_instance_key_name,    ║
        # ║         "message": "...",                                    ║
        # ║         "probes": ["<your-evidence-source>"],                ║
        # ║     }                                                        ║
        # ║     result["success"] = True                                 ║
        # ╚══════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            key_name = args.name
            result["instance_id"] = "dummy-vm-0001"
            result["public_ip"] = "203.0.113.10"
            result["private_ip"] = "10.0.0.10"
            result["key_file"] = "/tmp/dummy-key.pem"
            result["vpc_id"] = "dummy-vpc-0001"
            result["security_group_id"] = "dummy-sg-0001"
            result["requested_key_name"] = key_name
            result["key_name"] = key_name
            result["instance_type"] = args.instance_type
            result["state"] = "running"
            result["tests"] = {
                "specified_key": {
                    "passed": True,
                    "message": f"Instance uses requested key '{key_name}'",
                    "probes": ["instance_key_name"],
                }
            }
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's VM launch logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
