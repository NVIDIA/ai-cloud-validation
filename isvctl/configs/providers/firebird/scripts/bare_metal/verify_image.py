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

"""Verify the OS image a running Firebird bare-metal server was provisioned with.

Usage:
    python verify_image.py --instance-id bm.xxx [--expected-image-id image.xxx]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "instance_state": "running",
    "state": "running",
    "image_id": "image.xxx",
    "image_name": "ubuntu-22.04"
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, to_state


def main() -> int:
    """Read the BM's provisioned image and resolve its name.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Verify Firebird BM image")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--expected-image-id", help="Image the BM must report (image.ULID)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
    }

    try:
        client = FirebirdClient()
        bm = client.get_bm(args.instance_id)
        image_id = (bm.get("spec") or {}).get("imageId", "")
        result["instance_state"] = to_state(bm)
        result["state"] = result["instance_state"]
        result["image_id"] = image_id
        if not image_id:
            raise RuntimeError(f"BM {args.instance_id} reports no provisioned image")
        if args.expected_image_id and image_id != args.expected_image_id:
            raise RuntimeError(f"BM {args.instance_id} runs image {image_id}, expected {args.expected_image_id}")

        # A project-scoped listing also returns tenant-wide images, whichever scope the image lives in.
        images = client.paginate(f"/compute/images/{quote(client.project_id)}", "items")
        image = next((i for i in images if i.get("id") == image_id), {})
        result["image_name"] = image.get("name")
        result["image_version"] = image.get("version")
        if not image:
            raise RuntimeError(f"Image {image_id} not visible to project {client.project_id}")
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
