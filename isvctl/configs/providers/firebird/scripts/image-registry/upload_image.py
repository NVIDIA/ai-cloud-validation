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

"""Upload a custom OS image to the Firebird image registry (BOOT01-01).

Streams the image from ``--image-url`` (http(s), ``file://``, or a local path)
through the API's multipart upload into the run's project scope - no temporary
copy - with a SHA-256 checksum, then confirms the image is registered.

The contract's storage fields carry what the API does expose: ``storage_bucket``
is the scope the image is stored under (the project) and ``disk_ids`` holds
the upload's ``objectId``, the permanent reference to the stored file.

The API accepts qcow2 or raw whole-disk (MBR/GPT) images, the formats bare
metal can boot; others (vmdk, ...) are refused up front. When the image
upload API is not enabled the step is a structured skip.

Usage:
    python upload_image.py --image-url https://.../noble-server-cloudimg-amd64.img --image-format qcow2

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "image_id": "image.xxx",
    "image_name": "isv-ir-a1b2c3",
    "image_format": "qcow2",
    "storage_bucket": "project.xxx",
    "disk_ids": ["upload.xxx"],
    "upload_id": "upload.xxx",
    "size_bytes": 612368384
}
"""

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import images
from common.firebird_client import USER_AGENT, FirebirdApiError, FirebirdClient, is_not_registered, log
from common.service_accounts import unique_name


def open_source(url: str, stack: ExitStack) -> tuple[BinaryIO, int, str]:
    """Open the image source; return (stream, size in bytes, file name)."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https"):
        response = stack.enter_context(urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=120))
        size = int(response.headers.get("Content-Length") or 0)
        if size <= 0:
            raise RuntimeError(f"{url} did not report a Content-Length; download it and pass the local path")
        return response, size, Path(parts.path).name or "image"
    path = Path(unquote(parts.path) if parts.scheme == "file" else url)
    return stack.enter_context(path.open("rb")), path.stat().st_size, path.name


def main() -> int:
    """Upload the image and emit JSON.

    Returns:
        0 on success or skip, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Upload a custom OS image to Firebird")
    parser.add_argument("--image-url", required=True, help="Image source: http(s) URL, file:// URL, or local path")
    parser.add_argument("--image-format", required=True, help="Image format (qcow2 or raw)")
    parser.add_argument("--name-prefix", default="isv-ir", help="Image name prefix (a random suffix is added)")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for the upload to complete")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "image_id": "",
        "image_format": args.image_format,
        "storage_bucket": "",
        "disk_ids": [],
    }
    try:
        if args.image_format not in images.BM_FORMATS:
            result["error_type"] = "bad_input"
            raise RuntimeError(
                f"Firebird bare metal boots {' or '.join(images.BM_FORMATS)} images; "
                f"{args.image_format!r} is refused (set FIREBIRD_IMAGE_URL/FIREBIRD_IMAGE_FORMAT)"
            )
        client = FirebirdClient()
        scope = client.project_id
        name = unique_name(args.name_prefix)
        result.update(image_name=name, storage_bucket=scope)
        with ExitStack() as stack:
            stream, size, filename = open_source(args.image_url, stack)
            result["size_bytes"] = size
            log(f"Uploading {filename} ({size} bytes) as {name}...")
            try:
                finish = images.upload(
                    client, scope, stream, size, name=name, filename=filename, timeout=args.timeout, record=result
                )
            except FirebirdApiError as e:
                if "upload_id" not in result and is_not_registered(e):
                    result.update(success=True, skipped=True, skip_reason=images.NOT_ENABLED_REASON)
                    print(json.dumps(result, indent=2))
                    return 0
                raise
        result["disk_ids"] = [finish.get("objectId") or result["upload_id"]]
        result["image_id"] = images.resolve_image_id(client, scope, finish, name)
        image = client.request("GET", images.images_path(scope, result["image_id"])).get("image") or {}
        if image.get("id") != result["image_id"]:
            raise RuntimeError(f"image {result['image_id']} is not readable after upload")
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
