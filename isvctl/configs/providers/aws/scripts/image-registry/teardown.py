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

"""Teardown ISO validation resources.

Cleans up all resources created during ISO validation:
- EC2 instance
- AMI and snapshots
- S3 bucket and objects
- Key pair
- Security group
- IAM instance profile and role

Usage:
    python teardown.py --instance-id i-xxx --ami-id ami-xxx --bucket-name xxx ...

Output (JSON):
    {
        "success": true,
        "platform": "image_registry",
        "deleted": {
            "instance": "i-xxx",
            "ami": "ami-xxx",
            "snapshots": ["snap-xxx"],
            "bucket": "xxx",
            "key_pair": "xxx",
            "security_group": "sg-xxx",
            "instance_profile": "xxx"
        }
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors


def terminate_instance(ec2_client: Any, instance_id: str) -> bool:
    """Terminate EC2 instance."""
    if not instance_id:
        return True

    print(f"Terminating instance {instance_id}...", file=sys.stderr)
    try:
        ec2_client.terminate_instances(InstanceIds=[instance_id])

        # Wait for termination
        waiter = ec2_client.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": 30})
        print(f"Instance {instance_id} terminated", file=sys.stderr)
        return True
    except ClientError as e:
        if "InvalidInstanceID" in str(e):
            print(f"Instance {instance_id} not found (already deleted?)", file=sys.stderr)
            return True
        print(f"Failed to terminate instance: {e}", file=sys.stderr)
        return False


def delete_ami(ec2_client: Any, ami_id: str, snapshot_ids: list[str] | None = None) -> tuple[bool, list[str]]:
    """Deregister AMI and delete snapshots."""
    if not ami_id:
        return True, []

    deleted_snapshots = []

    # Deregister AMI
    print(f"Deregistering AMI {ami_id}...", file=sys.stderr)
    try:
        # Get snapshots if not provided
        if not snapshot_ids:
            response = ec2_client.describe_images(ImageIds=[ami_id])
            if response["Images"]:
                image = response["Images"][0]
                snapshot_ids = [
                    bdm["Ebs"]["SnapshotId"]
                    for bdm in image.get("BlockDeviceMappings", [])
                    if "Ebs" in bdm and "SnapshotId" in bdm["Ebs"]
                ]

        ec2_client.deregister_image(ImageId=ami_id)
        print(f"AMI {ami_id} deregistered", file=sys.stderr)
    except ClientError as e:
        if "InvalidAMIID" in str(e):
            print(f"AMI {ami_id} not found (already deleted?)", file=sys.stderr)
        else:
            print(f"Failed to deregister AMI: {e}", file=sys.stderr)
            return False, []

    # Delete snapshots
    time.sleep(5)  # Wait for deregister to complete
    for snap_id in snapshot_ids or []:
        print(f"Deleting snapshot {snap_id}...", file=sys.stderr)
        try:
            ec2_client.delete_snapshot(SnapshotId=snap_id)
            deleted_snapshots.append(snap_id)
            print(f"Snapshot {snap_id} deleted", file=sys.stderr)
        except ClientError as e:
            if "InvalidSnapshot" in str(e):
                print(f"Snapshot {snap_id} not found (already deleted?)", file=sys.stderr)
                deleted_snapshots.append(snap_id)
            else:
                print(f"Failed to delete snapshot {snap_id}: {e}", file=sys.stderr)

    return True, deleted_snapshots


def delete_bucket(s3_client: Any, bucket_name: str) -> bool:
    """Delete S3 bucket and all objects, including versioned objects and delete markers."""
    if not bucket_name:
        return True

    print(f"Deleting bucket {bucket_name}...", file=sys.stderr)
    try:
        # Delete all versioned objects and delete markers (handles versioning-enabled buckets)
        version_paginator = s3_client.get_paginator("list_object_versions")
        for page in version_paginator.paginate(Bucket=bucket_name):
            objects_to_delete = []
            for version in page.get("Versions", []):
                objects_to_delete.append({"Key": version["Key"], "VersionId": version["VersionId"]})
            for marker in page.get("DeleteMarkers", []):
                objects_to_delete.append({"Key": marker["Key"], "VersionId": marker["VersionId"]})
            if objects_to_delete:
                s3_client.delete_objects(Bucket=bucket_name, Delete={"Objects": objects_to_delete})

        # Delete any remaining non-versioned objects (unversioned bucket or missed objects)
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = page.get("Contents", [])
            if objects:
                s3_client.delete_objects(
                    Bucket=bucket_name, Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]}
                )

        # Delete bucket
        s3_client.delete_bucket(Bucket=bucket_name)
        print(f"Bucket {bucket_name} deleted", file=sys.stderr)
        return True
    except ClientError as e:
        if "NoSuchBucket" in str(e):
            print(f"Bucket {bucket_name} not found (already deleted?)", file=sys.stderr)
            return True
        print(f"Failed to delete bucket: {e}", file=sys.stderr)
        return False


def delete_key_pair(ec2_client: Any, key_name: str) -> bool:
    """Delete EC2 key pair."""
    if not key_name:
        return True

    print(f"Deleting key pair {key_name}...", file=sys.stderr)
    try:
        ec2_client.delete_key_pair(KeyName=key_name)
        print(f"Key pair {key_name} deleted", file=sys.stderr)
        return True
    except ClientError as e:
        print(f"Failed to delete key pair: {e}", file=sys.stderr)
        return False


def delete_security_group(ec2_client: Any, sg_id: str) -> bool:
    """Delete security group."""
    if not sg_id:
        return True

    print(f"Deleting security group {sg_id}...", file=sys.stderr)

    # Retry a few times as instance termination may not have released the SG yet
    for attempt in range(5):
        try:
            ec2_client.delete_security_group(GroupId=sg_id)
            print(f"Security group {sg_id} deleted", file=sys.stderr)
            return True
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "InvalidGroup.NotFound":
                print(f"Security group {sg_id} not found (already deleted?)", file=sys.stderr)
                return True
            if error_code == "DependencyViolation":
                print(f"Waiting for dependencies to be released (attempt {attempt + 1}/5)...", file=sys.stderr)
                time.sleep(10)
                continue
            print(f"Failed to delete security group: {e}", file=sys.stderr)
            return False

    print("Failed to delete security group after retries", file=sys.stderr)
    return False


def delete_instance_profile(iam_client: Any, profile_name: str) -> bool:
    """Delete IAM instance profile and role."""
    if not profile_name:
        return True

    print(f"Deleting instance profile {profile_name}...", file=sys.stderr)
    success = True

    # Remove role from profile
    try:
        iam_client.remove_role_from_instance_profile(InstanceProfileName=profile_name, RoleName=profile_name)
    except ClientError as e:
        if "NoSuchEntity" not in str(e):
            print(f"Failed to remove role from instance profile: {e}", file=sys.stderr)
            # Continue anyway, profile might not have the role

    # Delete instance profile
    try:
        iam_client.delete_instance_profile(InstanceProfileName=profile_name)
        print(f"Instance profile {profile_name} deleted", file=sys.stderr)
    except ClientError as e:
        if "NoSuchEntity" not in str(e):
            print(f"Failed to delete instance profile: {e}", file=sys.stderr)
            success = False

    # Detach policies from role
    try:
        iam_client.detach_role_policy(
            RoleName=profile_name, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        )
    except ClientError as e:
        if "NoSuchEntity" not in str(e):
            print(f"Failed to detach role policy: {e}", file=sys.stderr)
            # Continue anyway, policy might not be attached

    # Delete role
    try:
        iam_client.delete_role(RoleName=profile_name)
        print(f"IAM role {profile_name} deleted", file=sys.stderr)
    except ClientError as e:
        if "NoSuchEntity" not in str(e):
            print(f"Failed to delete IAM role: {e}", file=sys.stderr)
            success = False

    return success


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown ISO validation resources")
    parser.add_argument("--instance-id", help="EC2 instance ID to terminate")
    parser.add_argument("--ami-id", help="AMI ID to deregister")
    parser.add_argument("--snapshot-ids", help="Comma-separated snapshot IDs to delete")
    parser.add_argument("--bucket-name", help="S3 bucket to delete")
    parser.add_argument("--key-name", help="EC2 key pair to delete")
    parser.add_argument("--security-group-id", help="Security group ID to delete")
    parser.add_argument("--instance-profile", help="IAM instance profile to delete")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--skip-destroy", action="store_true", help="Skip teardown (for debugging)")
    args = parser.parse_args()

    if args.skip_destroy:
        print("Skipping teardown (--skip-destroy flag set)", file=sys.stderr)
        result = {"success": True, "platform": "image_registry", "skipped": True, "deleted": {}}
        print(json.dumps(result))
        return 0

    # Initialize clients
    session = boto3.Session(region_name=args.region)
    ec2_client = session.client("ec2")
    s3_client = session.client("s3")
    iam_client = session.client("iam")

    deleted = {}
    errors = []

    # Terminate instance first
    if args.instance_id:
        if terminate_instance(ec2_client, args.instance_id):
            deleted["instance"] = args.instance_id
        else:
            errors.append(f"Failed to terminate instance {args.instance_id}")

    # Delete AMI and snapshots
    if args.ami_id:
        snapshot_ids = args.snapshot_ids.split(",") if args.snapshot_ids else None
        success, deleted_snaps = delete_ami(ec2_client, args.ami_id, snapshot_ids)
        if success:
            deleted["ami"] = args.ami_id
            deleted["snapshots"] = deleted_snaps
        else:
            errors.append(f"Failed to delete AMI {args.ami_id}")

    # Delete S3 bucket
    if args.bucket_name:
        if delete_bucket(s3_client, args.bucket_name):
            deleted["bucket"] = args.bucket_name
        else:
            errors.append(f"Failed to delete bucket {args.bucket_name}")

    # Delete key pair
    if args.key_name:
        if delete_key_pair(ec2_client, args.key_name):
            deleted["key_pair"] = args.key_name
        else:
            errors.append(f"Failed to delete key pair {args.key_name}")

    # Delete security group
    if args.security_group_id:
        if delete_security_group(ec2_client, args.security_group_id):
            deleted["security_group"] = args.security_group_id
        else:
            errors.append(f"Failed to delete security group {args.security_group_id}")

    # Delete instance profile
    if args.instance_profile:
        if delete_instance_profile(iam_client, args.instance_profile):
            deleted["instance_profile"] = args.instance_profile
        else:
            errors.append(f"Failed to delete instance profile {args.instance_profile}")

    result = {
        "success": len(errors) == 0,
        "platform": "image_registry",
        "deleted": deleted,
    }
    if errors:
        result["errors"] = errors

    print(json.dumps(result))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
