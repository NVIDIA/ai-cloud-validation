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

"""Delete the image upload_image registered (``DELETE /compute/images/{scope}/{id}``).

Runs after the BM that installed it is deprovisioned: the API refuses to delete
an image a BM still references. Idempotent (an already-deleted image passes);
skips when the upload step was skipped.

Usage:
    python teardown_image.py --image-id image.xxx [--upload-skipped] [--skip-destroy]

Output JSON:
{"success": true, "platform": "image_registry", "resources_deleted": ["image:image.xxx"], "message": "..."}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import images
from common.firebird_client import FirebirdClient, log


def main() -> int:
    """Delete the uploaded image and emit JSON.

    Returns:
        0 when the image is gone (or nothing to do), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Delete the uploaded Firebird image")
    parser.add_argument("--image-id", default="", help="Image to delete (image.ULID)")
    parser.add_argument("--upload-skipped", action="store_true", help="The upload step was skipped")
    parser.add_argument("--skip-destroy", action="store_true", help="Keep the image")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for the delete")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "image_registry", "resources_deleted": []}
    if args.upload_skipped or args.skip_destroy:
        reason = "No custom image: upload_image was skipped" if args.upload_skipped else f"Kept {args.image_id}"
        result.update(success=True, skipped=True, skip_reason=reason)
        print(json.dumps(result, indent=2))
        return 0
    try:
        if args.image_id:
            client = FirebirdClient()
            deleted = images.delete_image(client, client.project_id, args.image_id, args.timeout)
            result["resources_deleted"].append(f"image:{args.image_id}")
            result["message"] = "Image deleted" if deleted else "Image already gone"
        else:
            result["message"] = "No image to delete"
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
