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

"""CRUD custom OS images using Compute Engine machine images.

Self-contained test: given an existing ``image_id`` (the source image
registered by the ``upload_image`` step), exercises the four lifecycle
operations the suite ``image_crud`` group's ``CrudOperationsCheck`` requires —
``get``, ``list``, ``create``, ``delete``:

  * GET    — ``ImagesClient.get`` describes the source image.
  * LIST   — ``ImagesClient.list`` enumerates owned images; the source must appear.
  * CREATE — ``ImagesClient.insert`` registers a NEW image from the source
             (``source_image``), waited to READY.
  * DELETE — ``ImagesClient.delete`` removes ONLY the created copy, never the
             source ``image_id`` (mirrors the AWS oracle: delete targets the copy).

Translates the AWS reference's ``copy_image`` (CREATE) and ``deregister_image``
(DELETE) onto the Compute Engine ImagesClient surface.

Required JSON output (suite ``image_crud`` group):
    {
        "success":   bool,
        "platform":  "image_registry",
        "image_id":  str,              # the source image name (echoed)
        "operations": {
            "get":    {"passed": bool, ...},
            "list":   {"passed": bool, ...},
            "create": {"passed": bool, "image_id": "<copy-name>"},
            "delete": {"passed": bool},
        },
    }

Usage:
    python crud_image.py --image-id <name> --region <region>

AWS reference implementation:
    ../../aws/scripts/image-registry/crud_image.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix, wait_for_global_op
from common.errors import handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import compute_v1


def _wait_for_image_ready(project: str, image_name: str, *, timeout: int = 300) -> str:
    """Poll images.get until READY; raise on FAILED / timeout. Returns final status."""
    client = compute_v1.ImagesClient()
    deadline = time.monotonic() + timeout
    while True:
        status = (client.get(project=project, image=image_name).status or "").upper()
        if status == "READY":
            return status
        if status == "FAILED":
            raise RuntimeError(f"image {image_name} entered FAILED state")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"image {image_name} did not reach READY (last={status!r}) in {timeout}s")
        time.sleep(5)


def test_get(client: compute_v1.ImagesClient, project: str, image_id: str) -> dict[str, Any]:
    """Describe the source image by name."""
    result: dict[str, Any] = {"passed": False}
    try:
        image = client.get(project=project, image=image_id)
        result["image_name"] = image.name
        result["state"] = image.status
        result["passed"] = True
        result["message"] = f"Described image {image_id}: status={image.status}"
    except gax.GoogleAPICallError as e:
        result["error"] = str(e)
    return result


def test_list(client: compute_v1.ImagesClient, project: str, image_id: str) -> dict[str, Any]:
    """List owned images and verify the source appears (exact name match)."""
    result: dict[str, Any] = {"passed": False}
    try:
        names = [img.name for img in client.list(project=project)]
        result["image_count"] = len(names)
        if image_id not in names:
            result["error"] = f"Image {image_id} not found in {len(names)} owned image(s)"
            return result
        result["passed"] = True
        result["message"] = f"Found {image_id} in {len(names)} owned image(s)"
    except gax.GoogleAPICallError as e:
        result["error"] = str(e)
    return result


def test_create(client: compute_v1.ImagesClient, project: str, image_id: str) -> dict[str, Any]:
    """Create a NEW image from the source image and wait until READY."""
    result: dict[str, Any] = {"passed": False}
    copy_name = unique_suffix("isv-ir-image-copy")
    try:
        copy = compute_v1.Image()
        copy.name = copy_name
        copy.source_image = f"projects/{project}/global/images/{image_id}"
        copy.description = "ISV image-registry validation image copy (createdby=isvtest)"
        op = client.insert(project=project, image_resource=copy)
        # insert accepted -> this step now owns copy_name. Stamp the cleanup
        # target on the insert ack, BEFORE the op-wait / READY poll, so a
        # wait-side timeout still lets cleanup delete the copy this step created
        # (NotFound is idempotent). A synchronous AlreadyExists/Conflict raises
        # HERE, before the stamp, so a name collision with a pre-existing image
        # is reported as a failure and never deleted.
        result["image_id"] = copy_name
        op_name = getattr(op, "name", "")
        if op_name:
            wait_for_global_op(project, op_name, timeout=300)
        _wait_for_image_ready(project, copy_name, timeout=300)
        result["image_name"] = copy_name
        result["passed"] = True
        result["message"] = f"Created image {copy_name} from {image_id}"
    except (gax.GoogleAPICallError, RuntimeError, TimeoutError) as e:
        result["error"] = str(e)
    return result


def test_delete(client: compute_v1.ImagesClient, project: str, copy_name: str) -> dict[str, Any]:
    """Delete the created copy (never the source). NotFound is idempotent success."""
    result: dict[str, Any] = {"passed": False}
    try:
        op = client.delete(project=project, image=copy_name)
        op_name = getattr(op, "name", "")
        if op_name:
            wait_for_global_op(project, op_name, timeout=300)
        result["passed"] = True
        result["message"] = f"Deleted image copy {copy_name}"
    except gax.NotFound:
        result["passed"] = True
        result["message"] = f"Image copy {copy_name} already absent (idempotent)"
    except gax.GoogleAPICallError as e:
        result["error"] = str(e)
    return result


@handle_gcp_errors
def main() -> int:
    """Run the get/list/create/delete image lifecycle and emit structured JSON."""
    parser = argparse.ArgumentParser(description="CRUD custom OS images (Compute Engine images)")
    parser.add_argument("--image-id", required=True, help="Source image name from the upload_image step")
    parser.add_argument("--region", required=True, help="GCP region (unused by global image ops; contract parity)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    client = compute_v1.ImagesClient()

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

    copy_name = ""
    try:
        get_result = test_get(client, project, args.image_id)
        result["operations"]["get"] = get_result
        if not get_result["passed"]:
            raise RuntimeError(f"get failed: {get_result.get('error')}")

        list_result = test_list(client, project, args.image_id)
        result["operations"]["list"] = list_result
        if not list_result["passed"]:
            raise RuntimeError(f"list failed: {list_result.get('error')}")

        create_result = test_create(client, project, args.image_id)
        result["operations"]["create"] = create_result
        copy_name = create_result.get("image_id", "")
        if not create_result["passed"]:
            raise RuntimeError(f"create failed: {create_result.get('error')}")

        delete_result = test_delete(client, project, copy_name)
        result["operations"]["delete"] = delete_result
        if not delete_result["passed"]:
            raise RuntimeError(f"delete failed: {delete_result.get('error')}")

        result["success"] = True
    except RuntimeError as e:
        result["error"] = str(e)
        # Cleanup the copy on partial failure so the run does not leak an image
        # (delete targets ONLY the copy, never the source image_id).
        if copy_name:
            try:
                client.delete(project=project, image=copy_name)
            except gax.GoogleAPICallError:
                pass

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
