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

"""Download a VM image, upload to cloud storage, and import as a machine image.

Provider-agnostic template - replace the TODO section with your platform's
image import pipeline (e.g. Glance for OpenStack, Compute Images for GCP,
Managed Images for Azure, etc.).

Required JSON output:
{
    "success":        bool       - true if image imported successfully,
    "platform":       str        - "image_registry",
    "image_id":       str        - ID of the imported machine image,
    "storage_bucket": str        - name of the storage bucket / container,
    "disk_ids":       list[str]  - snapshot or disk IDs created during import,
    "error":          str        - (optional) error message, present when success is false
}

Usage:
    python upload_image.py --image-url <url> --image-format vmdk --region <region>

AWS reference implementation:
    ../aws/image-registry/upload_image.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Upload and import VM image and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Upload and import VM image")
    parser.add_argument("--image-url", required=True, help="URL to download the VM image from")
    parser.add_argument(
        "--image-format",
        default="vmdk",
        help="Image format (vmdk, vhd, ova, raw, qcow2)",
    )
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "image_id": "",
        "storage_bucket": "",
        "disk_ids": [],
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Download the image from args.image_url                       ║
    # ║  2. Upload to cloud storage (object store, bucket, container)    ║
    # ║     -> result["storage_bucket"] = "<bucket-name>"                ║
    # ║  3. Import as a machine image (AMI, Glance image, etc.)          ║
    # ║     -> result["image_id"] = "<imported-image-id>"                ║
    # ║  4. Wait for import to complete                                  ║
    # ║  5. Record any snapshots or disks created                        ║
    # ║     -> result["disk_ids"] = ["<snapshot-or-disk-id>", ...]       ║
    # ║  6. Set result["success"] = True                                 ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["image_id"] = "dummy-image-0001"
        result["storage_bucket"] = "dummy-image-bucket"
        result["disk_ids"] = ["dummy-disk-0001"]
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's image import logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
