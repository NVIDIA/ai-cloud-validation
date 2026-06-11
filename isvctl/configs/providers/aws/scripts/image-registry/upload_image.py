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

"""Upload VMDK/VHD to S3 and import as AMI.

This script downloads (or uses local) a VM image, uploads to S3,
and imports it as an AMI via AWS VM Import.

Usage:
    python upload_image.py --image-url <url> [--image-format vmdk]
    python upload_image.py --local-path /path/to/image.vmdk

Output (JSON):
    {
        "success": true,
        "platform": "image_registry",
        "ami_id": "ami-xxx",
        "bucket_name": "isv-iso-import-xxx",
        "object_key": "image.vmdk",
        "snapshot_ids": ["snap-xxx"],
        "region": "us-west-2"
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError


def download_image(url: str, image_format: str = "vmdk") -> Path | None:
    """Download image from URL."""
    print(f"Downloading image from {url}...", file=sys.stderr)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{image_format}", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            response = requests.get(url, stream=True, timeout=600)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0
            last_progress = 0

            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    progress = int(downloaded * 100 / total_size) if total_size else 0
                    if progress >= last_progress + 10:
                        print(f"Download progress: {progress}%", file=sys.stderr)
                        last_progress = progress

            return tmp_path

    except requests.RequestException as e:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        print(f"Download failed: {e}", file=sys.stderr)
        return None


def create_vmimport_role(iam_client: Any) -> bool:
    """Create the vmimport service role required for VM Import."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "vmie.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": "vmimport"}},
            }
        ],
    }

    try:
        iam_client.create_role(
            RoleName="vmimport",
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Role for VM Import/Export",
        )
        print("Created vmimport role", file=sys.stderr)
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            print("vmimport role already exists", file=sys.stderr)
        else:
            print(f"Failed to create vmimport role: {e}", file=sys.stderr)
            return False

    return True


def attach_vmimport_policy(iam_client: Any, bucket_name: str) -> bool:
    """Attach the required policy to vmimport role."""
    role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket_name}", f"arn:aws:s3:::{bucket_name}/*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:ModifySnapshotAttribute",
                    "ec2:CopySnapshot",
                    "ec2:RegisterImage",
                    "ec2:Describe*",
                ],
                "Resource": "*",
            },
        ],
    }

    try:
        iam_client.put_role_policy(
            RoleName="vmimport",
            PolicyName="vmimport-policy",
            PolicyDocument=json.dumps(role_policy),
        )
        print("Attached vmimport policy", file=sys.stderr)
        # Wait for policy propagation
        time.sleep(10)
        return True
    except ClientError as e:
        print(f"Failed to attach vmimport policy: {e}", file=sys.stderr)
        return False


def upload_to_s3(s3_client: Any, local_path: Path, bucket_name: str) -> str | None:
    """Upload image to S3 bucket."""
    object_key = local_path.name

    # Create bucket if needed
    try:
        region = s3_client.meta.region_name
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": region})
        print(f"Created bucket: {bucket_name}", file=sys.stderr)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"Failed to create bucket: {e}", file=sys.stderr)
            return None

    # Upload file
    print(f"Uploading {local_path} to s3://{bucket_name}/{object_key}...", file=sys.stderr)
    try:
        s3_client.upload_file(str(local_path), bucket_name, object_key)
        print("Upload complete", file=sys.stderr)
        return object_key
    except ClientError as e:
        print(f"Upload failed: {e}", file=sys.stderr)
        return None


def import_as_ami(
    ec2_client: Any, bucket_name: str, object_key: str, image_format: str
) -> tuple[str | None, list[str]]:
    """Import image as AMI."""
    print("Starting VM import task...", file=sys.stderr)

    try:
        response = ec2_client.import_image(
            Description="ISV Lab imported image",
            DiskContainers=[
                {
                    "Description": "Root volume",
                    "Format": image_format.upper(),
                    "UserBucket": {"S3Bucket": bucket_name, "S3Key": object_key},
                }
            ],
            LicenseType="BYOL",
        )
        task_id = response["ImportTaskId"]
        print(f"Import task started: {task_id}", file=sys.stderr)
    except ClientError as e:
        print(f"Failed to start import: {e}", file=sys.stderr)
        return None, []

    # Wait for import to complete (can take 15-60 minutes)
    print("Waiting for import to complete (this may take 15-60 minutes)...", file=sys.stderr)
    max_wait = 3600  # 60 minutes
    start_time = time.time()
    last_status = ""

    while time.time() - start_time < max_wait:
        try:
            response = ec2_client.describe_import_image_tasks(ImportTaskIds=[task_id])
            task = response["ImportImageTasks"][0]
            status = task.get("Status", "unknown")
            progress = task.get("Progress", "")

            status_msg = f"{status} ({progress}%)" if progress else status
            if status_msg != last_status:
                print(f"Import status: {status_msg}", file=sys.stderr)
                last_status = status_msg

            if status == "completed":
                ami_id = task.get("ImageId")
                snapshot_ids = [
                    snap.get("SnapshotId") for snap in task.get("SnapshotDetails", []) if snap.get("SnapshotId")
                ]
                print(f"Import complete! AMI: {ami_id}", file=sys.stderr)
                return ami_id, snapshot_ids

            if status in ("deleted", "error"):
                error = task.get("StatusMessage", "Unknown error")
                print(f"Import failed: {error}", file=sys.stderr)
                return None, []

            time.sleep(30)

        except ClientError as e:
            print(f"Error checking import status: {e}", file=sys.stderr)
            time.sleep(30)

    print("Import timed out", file=sys.stderr)
    return None, []


def main() -> int:
    """CLI entry point for VM image upload and import.

    Parses arguments, initializes AWS clients, downloads or loads the image,
    creates the vmimport IAM role, uploads to S3, and imports as an AMI.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    parser = argparse.ArgumentParser(description="Upload and import VM image as AMI")
    parser.add_argument("--image-url", help="URL to download image from")
    parser.add_argument("--local-path", help="Local path to image file")
    parser.add_argument("--image-format", default="vmdk", help="Image format (vmdk, vhd, ova, raw)")
    parser.add_argument("--bucket-name", help="S3 bucket name (auto-generated if not provided)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    if not args.image_url and not args.local_path:
        args.image_url = (
            "https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.vmdk"
        )

    # Initialize clients
    session = boto3.Session(region_name=args.region)
    s3_client = session.client("s3")
    ec2_client = session.client("ec2")
    iam_client = session.client("iam")

    # Get or download image
    if args.local_path:
        image_path = Path(args.local_path).expanduser()
        if not image_path.exists():
            result = {"success": False, "error": f"File not found: {image_path}"}
            print(json.dumps(result))
            return 1
    else:
        image_path = download_image(args.image_url, args.image_format)
        if not image_path:
            result = {"success": False, "error": "Failed to download image"}
            print(json.dumps(result))
            return 1

    try:
        # Generate bucket name if not provided
        bucket_name = args.bucket_name or f"isv-iso-import-{uuid.uuid4().hex[:8]}"

        # Create vmimport role
        if not create_vmimport_role(iam_client):
            result = {"success": False, "error": "Failed to create vmimport role"}
            print(json.dumps(result))
            return 1

        if not attach_vmimport_policy(iam_client, bucket_name):
            result = {"success": False, "error": "Failed to attach vmimport policy"}
            print(json.dumps(result))
            return 1

        # Upload to S3
        object_key = upload_to_s3(s3_client, image_path, bucket_name)
        if not object_key:
            result = {"success": False, "error": "Failed to upload to S3"}
            print(json.dumps(result))
            return 1

        # Import as AMI
        ami_id, snapshot_ids = import_as_ami(ec2_client, bucket_name, object_key, args.image_format)
        if not ami_id:
            result = {"success": False, "error": "Failed to import as AMI"}
            print(json.dumps(result))
            return 1

        result = {
            "success": True,
            "platform": "image_registry",
            # Generic fields (provider-agnostic)
            "image_id": ami_id,
            "image_name": f"isv-imported-{ami_id}",
            "storage_bucket": bucket_name,
            "storage_path": object_key,
            "disk_ids": snapshot_ids,
            "image_format": args.image_format,
            "region": args.region,
            "image_state": "available",
            # AWS-specific fields (for reference)
            "ami_id": ami_id,
            "bucket_name": bucket_name,
            "object_key": object_key,
            "snapshot_ids": snapshot_ids,
        }
        print(json.dumps(result))
        return 0
    finally:
        if not args.local_path and image_path.exists():
            image_path.unlink()


if __name__ == "__main__":
    sys.exit(main())
