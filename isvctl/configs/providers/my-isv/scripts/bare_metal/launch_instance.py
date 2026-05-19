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

"""Launch bare-metal GPU instance - TEMPLATE (replace with your platform implementation).

This script is called during the "setup" phase. It must:
  1. Create an SSH key pair (or retrieve existing credentials)
  2. Create a security group / firewall rule allowing SSH
  3. Launch a bare-metal GPU instance
  4. Wait for the instance to reach "running" state (bare-metal boot is
     slower than VM: hardware POST, BIOS, OS boot without hypervisor)
  5. Retrieve the public IP address
  6. Print a JSON object to stdout

Instance reuse (dev workflow):
    Set BM_INSTANCE_ID and BM_KEY_FILE env vars to skip launching and
    describe an existing instance instead. This allows fast iteration on
    validations without reprovisioning.

Required JSON output fields:
  {
    "success": true,               # boolean - did the operation succeed?
    "platform": "bm",              # string  - always "bm"
    "instance_id": "...",          # string  - unique instance identifier
    "public_ip": "54.x.x.x",       # string  - public IP for SSH access
    "private_ip": "10.0.0.20",     # string  - private IP (read by DhcpIpManagementCheck)
    "key_file": "/tmp/key.pem",    # string  - path to SSH private key
    "vpc_id": "vpc-xxx",           # string  - network/VPC identifier
    "state": "running",            # string  - must be "running" (read by InstanceStateCheck)
    "security_group_id": "sg-xxx", # string  - security group / firewall ID
    "key_name": "my-key",          # string  - key pair name
    "instance_type": "..."         # string  - echoed from --instance-type
  }

On failure, set "success": false and include an "error" field.

Usage:
    python launch_instance.py --name isv-bm-test-gpu --instance-type <type> --region <region>

    # Reuse existing instance:
    BM_INSTANCE_ID=i-xxx BM_KEY_FILE=/tmp/key.pem python launch_instance.py \
        --name isv-bm-test-gpu --instance-type <type> --region <region>

Reference implementation: ../../aws/bare_metal/launch_instance.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Launch a bare-metal GPU instance and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Launch bare-metal GPU instance (template)")
    parser.add_argument("--name", default="isv-bm-test-gpu", help="Instance name tag")
    parser.add_argument("--instance-type", required=True, help="Bare-metal instance type")
    parser.add_argument("--region", required=True, help="Cloud region")

    def _arg_error(message: str) -> None:
        print(json.dumps({"success": False, "platform": "bm", "error": message}, indent=2))
        raise SystemExit(2)

    parser.error = _arg_error  # type: ignore[assignment]
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": "",
        "public_ip": "",
        "private_ip": "",
        "key_file": "",
        "vpc_id": "",
        "state": "",
        "security_group_id": "",
        "key_name": "",
        "instance_type": "",
    }

    # ── Dev workflow: reuse existing instance ──────────────────────────
    # Demo mode (ISVCTL_DEMO_MODE=1) takes precedence so `make demo-test` is
    # deterministic even when these env vars happen to be set in a dev shell.
    if (not DEMO_MODE) and os.environ.get("BM_INSTANCE_ID") and os.environ.get("BM_KEY_FILE"):
        instance_id = os.environ["BM_INSTANCE_ID"]
        key_file = os.environ["BM_KEY_FILE"]
        print(f"Reusing existing instance {instance_id}", file=sys.stderr)

        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Describe existing instance and populate result        ║
        # ║                                                              ║
        # ║  Example (pseudocode):                                       ║
        # ║    client = MyCloudClient(region=args.region)                ║
        # ║    info = client.describe_instance(instance_id)              ║
        # ║    result["instance_id"] = instance_id                       ║
        # ║    result["public_ip"] = info.public_ip                      ║
        # ║    result["key_file"] = key_file                             ║
        # ║    result["vpc_id"] = info.vpc_id                            ║
        # ║    result["state"] = info.state                              ║
        # ║    result["security_group_id"] = info.security_group_id      ║
        # ║    result["key_name"] = info.key_name                        ║
        # ║    result["success"] = info.state == "running"               ║
        # ╚══════════════════════════════════════════════════════════════╝

        result["instance_id"] = instance_id
        result["key_file"] = key_file
        result["error"] = "Not implemented - replace with your platform's instance describe logic"
        print(json.dumps(result, indent=2))
        return 0 if result["success"] else 1

    # ── Normal launch flow ────────────────────────────────────────────

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's provisioning      ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║                                                                  ║
    # ║    1. Create key pair:                                           ║
    # ║       key = client.create_key_pair(f"{args.name}-key")           ║
    # ║       key_file = f"/tmp/{args.name}-key.pem"                     ║
    # ║       write key.private_key to key_file (mode 0o600)             ║
    # ║       result["key_name"] = key.name                              ║
    # ║       result["key_file"] = key_file                              ║
    # ║                                                                  ║
    # ║    2. Create security group (SSH access):                        ║
    # ║       sg = client.create_security_group(                         ║
    # ║           name=f"{args.name}-sg",                                ║
    # ║           rules=[{"port": 22, "cidr": "<ADMIN_CIDR>"}]           ║
    # ║       )                                                          ║
    # ║       # WARNING: "0.0.0.0/0" allows SSH from any IP and is      ║
    # ║       # overly permissive for production. Prefer restricting     ║
    # ║       # the CIDR to a known admin IP range, or route access      ║
    # ║       # through a bastion host / VPN.                            ║
    # ║       result["security_group_id"] = sg.id                        ║
    # ║                                                                  ║
    # ║    3. Launch bare-metal GPU instance:                            ║
    # ║       instance = client.launch_instance(                         ║
    # ║           name=args.name,                                        ║
    # ║           instance_type=args.instance_type,                      ║
    # ║           key_name=key.name,                                     ║
    # ║           security_group_ids=[sg.id],                            ║
    # ║       )                                                          ║
    # ║       result["instance_id"] = instance.id                        ║
    # ║                                                                  ║
    # ║    4. Wait for running (BM takes longer: hardware POST/BIOS):    ║
    # ║       client.wait_until_running(instance.id, timeout=1200)       ║
    # ║       info = client.describe_instance(instance.id)               ║
    # ║       result["public_ip"] = info.public_ip                       ║
    # ║       result["vpc_id"] = info.vpc_id                             ║
    # ║       result["state"] = "running"                                ║
    # ║       result["success"] = True                                   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["instance_id"] = "dummy-bm-0001"
        result["public_ip"] = "203.0.113.20"
        result["private_ip"] = "10.0.0.20"
        result["key_file"] = "/tmp/dummy-bm-key.pem"
        result["vpc_id"] = "dummy-vpc-bm-0001"
        result["security_group_id"] = "dummy-sg-bm-0001"
        result["key_name"] = args.name
        result["instance_type"] = args.instance_type
        result["state"] = "running"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's instance launch logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
