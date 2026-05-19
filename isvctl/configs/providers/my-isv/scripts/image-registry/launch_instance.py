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

"""Launch a GPU instance from an imported machine image.

Provider-agnostic template - replace the TODO section with your platform's
compute instance creation calls.

Required JSON output:
{
    "success":           bool  - true if instance is running,
    "platform":          str   - "image_registry",
    "instance_id":       str   - unique instance identifier,
    "public_ip":         str   - public IP address for SSH,
    "key_path":          str   - local path to the SSH private key,
    "state":             str   - "running" (read by InstanceStateCheck),
    "key_name":          str   - name of the key pair,
    "security_group_id": str   - security group / firewall rule ID,
    "instance_profile":  str   - IAM / instance profile name,
    "error":             str   - (optional) error message, present when success is false
}

Usage:
    python launch_instance.py --image-id <image-id> --instance-type <type> --region <region>

AWS reference implementation:
    ../aws/image-registry/launch_instance.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Launch GPU instance from imported image and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Launch GPU instance from imported image")
    parser.add_argument("--image-id", required=True, help="Imported machine image ID")
    parser.add_argument("--instance-type", required=True, help="Instance type / flavor")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "instance_id": "",
        "public_ip": "",
        "key_path": "",
        "state": "",
        "key_name": "",
        "security_group_id": "",
        "instance_profile": "",
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Create an SSH key pair                                       ║
    # ║     -> result["key_name"] = "<key-pair-name>"                    ║
    # ║     -> result["key_path"] = "<path-to-private-key>"              ║
    # ║  2. Create a security group / firewall rule (allow SSH)          ║
    # ║     -> result["security_group_id"] = "<sg-id>"                   ║
    # ║  3. (Optional) Create an instance profile / service account      ║
    # ║     -> result["instance_profile"] = "<profile-name>"             ║
    # ║  4. Launch GPU instance from the imported image                  ║
    # ║     -> result["instance_id"] = "<instance-id>"                   ║
    # ║  5. Wait for the instance to reach "running" state               ║
    # ║     -> result["state"] = "running"                               ║
    # ║  6. Get the public IP                                            ║
    # ║     -> result["public_ip"] = "<ip-address>"                      ║
    # ║  7. Set result["success"] = True                                 ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["instance_id"] = "dummy-img-instance-0001"
        result["public_ip"] = "203.0.113.40"
        result["key_path"] = "/tmp/dummy-img-key.pem"
        result["key_name"] = "dummy-img-key"
        result["security_group_id"] = "dummy-sg-img"
        result["instance_profile"] = "dummy-img-profile"
        result["state"] = "running"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's instance launch logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
