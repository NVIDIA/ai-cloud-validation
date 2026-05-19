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

"""Install an OS image on a bare-metal system.

Provider-agnostic template - replace the TODO section with your platform's
bare-metal provisioning API calls to install an OS from a registry image.

Required JSON output:
{
    "success":        bool  - true if BM instance provisioned and running,
    "platform":       str   - "image_registry",
    "instance_id":    str   - bare-metal instance identifier,
    "image_id":       str   - image used for provisioning,
    "instance_state": str   - "running",
    "error":          str   - (optional) error message, present when success is false
}

Usage:
    python install_image_bm.py --image-id img-xxx --region <region>

AWS reference implementation:
    ../aws/image-registry/install_image_bm.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Install OS image on bare-metal and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Install OS image on bare-metal")
    parser.add_argument("--image-id", required=True, help="OS image ID from the registry")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "instance_id": "",
        "image_id": args.image_id,
        "instance_state": "",
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Provision a bare-metal node from the OS image                ║
    # ║     instance = provision_bm(                                     ║
    # ║         image_id=args.image_id, region=args.region,              ║
    # ║     )                                                            ║
    # ║     result["instance_id"] = instance.id                          ║
    # ║                                                                  ║
    # ║  2. Wait for the node to reach "running" state                   ║
    # ║     wait_for_running(instance.id)                                ║
    # ║     result["instance_state"] = "running"                         ║
    # ║                                                                  ║
    # ║  3. Set result["success"] = True                                 ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["instance_id"] = "dummy-bm-img-0001"
        result["instance_state"] = "running"
        result["state"] = "running"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's BM image install logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
