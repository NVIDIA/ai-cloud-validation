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

"""CRUD custom OS images - TEMPLATE (replace with your platform implementation).

This script validates get, list, create, and delete operations on OS images.
Given an existing image_id (from the upload_image step), it should:

  1. GET    - Retrieve image details by ID
  2. LIST   - List images and verify the target appears
  3. CREATE - Create a new image (e.g., copy/clone the source)
  4. DELETE - Delete the created copy

Required JSON output:
{
    "success": true,
    "platform": "image_registry",
    "image_id": "<source-image-id>",
    "operations": {
        "get":    {"passed": true, "image_name": "...", "state": "available"},
        "list":   {"passed": true, "image_count": 2},
        "create": {"passed": true, "image_id": "<copy-image-id>"},
        "delete": {"passed": true}
    }
}

Usage:
    python crud_image.py --image-id <id> --region <region>

Reference implementation: ../../aws/image-registry/crud_image.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """CRUD custom OS images (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="CRUD custom OS images (template)")
    parser.add_argument("--image-id", required=True, help="Source image ID from upload_image step")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "image_id": args.image_id,
        "operations": {
            "get": {"passed": False},
            "list": {"passed": False},
            "create": {"passed": False},
            "delete": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's image CRUD        ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    client = MyImageRegistryClient(region=args.region)            ║
    # ║    image  = client.get_image(args.image_id)                      ║
    # ║    result["operations"]["get"] = {                               ║
    # ║        "passed": True, "image_name": image.name,                 ║
    # ║        "state": image.status}                                    ║
    # ║    images = client.list_images()                                 ║
    # ║    result["operations"]["list"] = {                              ║
    # ║        "passed": True, "image_count": len(images)}               ║
    # ║    copy   = client.copy_image(args.image_id, name="isv-copy")    ║
    # ║    result["operations"]["create"] = {                            ║
    # ║        "passed": True, "image_id": copy.id}                      ║
    # ║    client.delete_image(copy.id)                                  ║
    # ║    result["operations"]["delete"] = {"passed": True}             ║
    # ║    result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["operations"] = {
            "get": {"passed": True, "image_name": "dummy-image", "state": "available"},
            "list": {"passed": True, "image_count": 1},
            "create": {"passed": True, "image_id": "dummy-image-copy"},
            "delete": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's image CRUD logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
