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

"""Describe bare-metal instance - TEMPLATE (replace with your platform implementation).

This script is called during the "test" phase. It must:
  1. Query your platform for the current instance state
  2. Return network info (public IP) and pass through the SSH key file
  3. Print a JSON object to stdout

This is a lightweight step that validations bind to. SSH, GPU, and host-OS
checks run against this step's output, ensuring they execute in the test
phase (so teardown always runs, even if tests fail).

Required JSON output fields (read by InstanceStateCheck and SSH-based host checks):
  {
    "success": true,           # boolean - did the query succeed?
    "platform": "bm",          # string  - always "bm"
    "instance_id": "...",      # string  - instance identifier
    "state": "running",        # string  - current state
    "public_ip": "54.x.x.x",   # string  - public IP for SSH access
    "key_file": "/tmp/key.pem" # string  - path to SSH private key
  }

On failure, set "success": false and include an "error" field.

Usage:
    python describe_instance.py --instance-id <id> --region <region> --key-file /tmp/key.pem

Reference implementation: ../../aws/bare_metal/describe_instance.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Describe bare-metal instance (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Describe bare-metal instance (template)")
    parser.add_argument("--instance-id", required=True, help="Instance identifier")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "state": "",
        "public_ip": "",
        "key_file": args.key_file,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's describe logic    ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyCloudClient(region=args.region)                    ║
    # ║    info = client.describe_instance(args.instance_id)             ║
    # ║                                                                  ║
    # ║    result["state"] = info.state                # "running"       ║
    # ║    result["public_ip"] = info.public_ip        # for SSH         ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["state"] = "running"
        result["public_ip"] = "203.0.113.20"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's instance describe logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
