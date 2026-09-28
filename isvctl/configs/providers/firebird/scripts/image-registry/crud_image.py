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

"""Custom OS image CRUD (BOOT03-02).

Runs the full lifecycle on an image of its own, so the image the run uploaded
for the BM install is never touched (the API refuses to delete an image a BM
references, and install_image_bm needs it):
  create  upload a 1 MiB raw disk image with an MBR partition table (the
          smallest file the API accepts; creating an image is uploading one)
  get     ``GET /compute/images/{scope}/{id}``
  list    ``GET /compute/images/{scope}`` lists it
  delete  ``DELETE /compute/images/{scope}/{id}``, then the image reads 404
The image is deleted on every path. Skips when the upload service is not enabled.

Usage:
    python crud_image.py

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "image_id": "image.xxx",
    "operations": {"create": {"passed": true}, "get": {"passed": true},
                   "list": {"passed": true}, "delete": {"passed": true}}
}
"""

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import images
from common.firebird_client import FirebirdApiError, FirebirdClient, is_not_registered, log
from common.network import exists
from common.service_accounts import unique_name

OPERATIONS = ("create", "get", "list", "delete")
PROBE_IMAGE_SIZE = 1024 * 1024


def probe_disk_image(size: int = PROBE_IMAGE_SIZE) -> bytes:
    """Return a raw whole-disk image: an MBR with one Linux partition, zeros elsewhere."""
    disk = bytearray(size)
    sectors = size // 512
    entry = bytearray(16)
    entry[4] = 0x83  # Linux partition type; boot indicator (byte 0) stays 0x00
    entry[8:12] = (1).to_bytes(4, "little")  # first LBA
    entry[12:16] = (sectors - 1).to_bytes(4, "little")  # sector count
    disk[446:462] = entry
    disk[510:512] = b"\x55\xaa"
    return bytes(disk)


def main() -> int:
    """Create, read, list, and delete a probe image; emit JSON.

    Returns:
        0 when every operation passed or the upload service is off, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird custom image CRUD test")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait for upload and delete")
    args = parser.parse_args()

    operations: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "image_registry", "image_id": "", "operations": operations}
    client = None
    record: dict[str, Any] = {}
    try:
        client = FirebirdClient()
        scope = client.project_id
        name = unique_name("isv-ir-crud")
        data = probe_disk_image()
        try:
            stream = io.BytesIO(data)
            finish = images.upload(
                client, scope, stream, len(data), name=name, filename=f"{name}.raw", timeout=args.timeout, record=record
            )
        except FirebirdApiError as e:
            if "upload_id" not in record and is_not_registered(e):
                result.update(success=True, skipped=True, skip_reason=images.NOT_ENABLED_REASON)
                print(json.dumps(result, indent=2))
                return 0
            raise
        image_id = record["image_id"] = images.resolve_image_id(client, scope, finish, name)
        result["image_id"] = image_id
        operations["create"] = {"passed": True, "message": f"uploaded {len(data)} bytes as {image_id}"}

        image = client.request("GET", images.images_path(scope, image_id)).get("image") or {}
        operations["get"] = {"passed": image.get("id") == image_id and image.get("name") == name}
        listed = [i.get("id") for i in client.paginate(images.images_path(scope), "items")]
        operations["list"] = {"passed": image_id in listed, "message": f"{len(listed)} image(s) in {scope}"}

        images.delete_image(client, scope, image_id, args.timeout)
        record["image_id"] = ""
        gone = not exists(client, images.images_path(scope, image_id))
        operations["delete"] = {"passed": gone, "message": "image reads 404 after delete"}
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        if client and record.get("image_id"):
            try:
                images.delete_image(client, client.project_id, record["image_id"], args.timeout)
            except Exception as e:
                result["cleanup_errors"] = [f"image:{record['image_id']}: {e}"]

    for op in OPERATIONS:
        operations.setdefault(op, {"passed": False, "error": result.get("error", "not run")})
        if not operations[op]["passed"]:
            operations[op].setdefault("error", f"{op} did not verify")
    result["success"] = all(op["passed"] for op in operations.values()) and "cleanup_errors" not in result
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
