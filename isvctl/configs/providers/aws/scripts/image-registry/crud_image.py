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

"""CRUD custom OS images using EC2 AMIs.

Self-contained test: given an existing image_id (from the upload_image step),
performs get, list, create (copy), and delete operations.

Usage:
    python crud_image.py --image-id ami-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "image_id": "ami-xxx",
    "operations": {
        "get":    {"passed": true, "image_name": "...", "state": "available"},
        "list":   {"passed": true, "image_count": 2},
        "create": {"passed": true, "image_id": "ami-copy-xxx"},
        "delete": {"passed": true}
    }
}
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors


def test_get(ec2: Any, image_id: str) -> dict[str, Any]:
    """Describe a single image by ID."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_images(ImageIds=[image_id])
        images = response.get("Images", [])
        if not images:
            result["error"] = f"Image {image_id} not found"
            return result

        image = images[0]
        result["image_name"] = image.get("Name", "")
        result["state"] = image.get("State", "")
        result["architecture"] = image.get("Architecture", "")
        result["image_type"] = image.get("ImageType", "")
        result["passed"] = True
        result["message"] = f"Described image {image_id}: state={image.get('State')}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_list(ec2: Any, image_id: str) -> dict[str, Any]:
    """List images owned by self and verify the target image appears."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_images(Owners=["self"])
        images = response.get("Images", [])
        image_ids = [img["ImageId"] for img in images]

        result["image_count"] = len(images)

        if image_id not in image_ids:
            result["error"] = f"Image {image_id} not found in owned images list ({len(images)} images)"
            return result

        result["passed"] = True
        result["message"] = f"Found {image_id} in {len(images)} owned image(s)"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_create(ec2: Any, image_id: str, region: str) -> dict[str, Any]:
    """Create a new image by copying an existing one."""
    result: dict[str, Any] = {"passed": False}
    copy_name = f"isvtest-copy-{image_id}"
    try:
        response = ec2.copy_image(
            Name=copy_name,
            SourceImageId=image_id,
            SourceRegion=region,
            Description="ISV validation test - image copy",
        )
        copy_id = response["ImageId"]
        result["image_id"] = copy_id
        result["image_name"] = copy_name

        # Wait for copy to become available
        print(f"Waiting for copied image {copy_id} to become available...", file=sys.stderr)
        max_wait = 300
        start = time.time()
        while time.time() - start < max_wait:
            desc = ec2.describe_images(ImageIds=[copy_id])
            state = desc["Images"][0]["State"] if desc["Images"] else "unknown"
            if state == "available":
                result["passed"] = True
                result["message"] = f"Copied image {copy_id} is available"
                return result
            if state == "failed":
                result["error"] = f"Copied image {copy_id} entered failed state"
                return result
            time.sleep(10)

        result["error"] = f"Copied image {copy_id} did not become available within {max_wait}s"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_delete(ec2: Any, image_id: str) -> dict[str, Any]:
    """Delete (deregister) an image and its backing snapshots."""
    result: dict[str, Any] = {"passed": False}
    try:
        # Get snapshot IDs before deregistering
        desc = ec2.describe_images(ImageIds=[image_id])
        snapshot_ids = []
        if desc["Images"]:
            for bdm in desc["Images"][0].get("BlockDeviceMappings", []):
                snap_id = bdm.get("Ebs", {}).get("SnapshotId")
                if snap_id:
                    snapshot_ids.append(snap_id)

        ec2.deregister_image(ImageId=image_id)

        # Delete backing snapshots
        for snap_id in snapshot_ids:
            try:
                ec2.delete_snapshot(SnapshotId=snap_id)
            except ClientError:
                pass  # Best-effort snapshot cleanup

        result["passed"] = True
        result["message"] = f"Deregistered {image_id}, deleted {len(snapshot_ids)} snapshot(s)"
    except ClientError as e:
        result["error"] = str(e)
    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD custom OS images (AMI)")
    parser.add_argument("--image-id", required=True, help="Source image ID from upload_image step")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

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

    copy_id = ""

    try:
        # GET
        get_result = test_get(ec2, args.image_id)
        result["operations"]["get"] = get_result
        if not get_result["passed"]:
            raise RuntimeError(f"Get failed: {get_result.get('error')}")

        # LIST
        list_result = test_list(ec2, args.image_id)
        result["operations"]["list"] = list_result
        if not list_result["passed"]:
            raise RuntimeError(f"List failed: {list_result.get('error')}")

        # CREATE (copy)
        create_result = test_create(ec2, args.image_id, args.region)
        result["operations"]["create"] = create_result
        if not create_result["passed"]:
            raise RuntimeError(f"Create failed: {create_result.get('error')}")
        copy_id = create_result["image_id"]

        # DELETE (the copy, not the original)
        delete_result = test_delete(ec2, copy_id)
        result["operations"]["delete"] = delete_result
        if not delete_result["passed"]:
            raise RuntimeError(f"Delete failed: {delete_result.get('error')}")

        result["success"] = True

    except RuntimeError as e:
        result["error"] = str(e)
        # Cleanup on partial failure
        if copy_id:
            try:
                ec2.deregister_image(ImageId=copy_id)
            except ClientError:
                pass

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
