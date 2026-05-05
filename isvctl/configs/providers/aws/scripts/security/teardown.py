#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Security test teardown (AWS reference).

Each individual test script handles its own cleanup. This teardown step
is a safety net that scans for leftover resources that owned-prefix
test scripts may have leaked on a hard crash:

* IAM users
    * ``isv-sa-test-*``     - sa_credential_test.py
    * ``isv-sec02-test-*``  - short_lived_credentials_test.py
    * ``isv-sec04-test-*``  - least_privilege_test.py
    * ``isv-sec11-test-*``  - tenant_isolation_test.py
* SEC11 fixture (``isv-sec11-test-*`` prefix + ``CreatedBy=isvtest`` tag):
    * EC2 instances, EBS volumes, security groups, subnets, VPCs
    * KMS aliases (and the keys they target)
    * S3 buckets

All deletes go through ``delete_with_retry`` so a transient throttling
or endpoint reset does not leak resources on the next loop iteration.

Usage:
    python teardown.py --region us-west-2
    python teardown.py --region us-west-2 --skip-destroy
"""

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError, WaiterError
from common.errors import delete_with_retry, handle_aws_errors

OWNED_USER_PREFIXES: tuple[str, ...] = ("isv-sa-test-", "isv-sec02-test-", "isv-sec04-test-", "isv-sec11-test-")

# SEC11 tenant-isolation fixture prefix. Used to scope EC2/KMS/S3 sweeps
# so we never touch resources outside the suite's namespace.
SEC11_PREFIX = "isv-sec11-test-"
SEC11_KMS_ALIAS_PREFIX = f"alias/{SEC11_PREFIX}"
ISVTEST_TAG_FILTER = [
    {"Name": "tag:CreatedBy", "Values": ["isvtest"]},
]


def _user_has_isvtest_tag(iam: Any, username: str) -> bool:
    """Return True when the IAM user is tagged as owned by isvtest."""
    try:
        kwargs: dict[str, Any] = {"UserName": username}
        while True:
            response = iam.list_user_tags(**kwargs)
            for tag in response.get("Tags", []):
                if tag.get("Key") == "CreatedBy" and tag.get("Value") == "isvtest":
                    return True
            if not response.get("IsTruncated"):
                return False
            kwargs["Marker"] = response.get("Marker")
    except ClientError:
        return False


def _cleanup_owned_user(iam: Any, username: str) -> list[str]:
    """Delete one owned IAM user, its access keys, and any inline policies.

    Inline policies must be detached before ``DeleteUser`` succeeds; SEC02
    and SEC11 test users carry one. SA-credential test users have no
    inline policies, so the inline-policy pass is a no-op for them.
    """
    cleanup_errors: list[str] = []
    keys: list[dict[str, Any]] = []
    inline_policies: list[str] = []

    try:
        keys = iam.list_access_keys(UserName=username)["AccessKeyMetadata"]
    except ClientError as e:
        cleanup_errors.append(f"list access keys for {username}: {e}")

    for key in keys:
        access_key_id = key["AccessKeyId"]
        try:
            iam.delete_access_key(UserName=username, AccessKeyId=access_key_id)
        except ClientError as e:
            cleanup_errors.append(f"delete access key {access_key_id} for {username}: {e}")

    try:
        inline_policies = iam.list_user_policies(UserName=username).get("PolicyNames", [])
    except ClientError as e:
        cleanup_errors.append(f"list inline policies for {username}: {e}")

    for policy_name in inline_policies:
        try:
            iam.delete_user_policy(UserName=username, PolicyName=policy_name)
        except ClientError as e:
            cleanup_errors.append(f"delete inline policy {policy_name} for {username}: {e}")

    try:
        iam.delete_user(UserName=username)
    except ClientError as e:
        cleanup_errors.append(f"delete user {username}: {e}")

    return cleanup_errors


def _resource_has_sec11_name(tags: list[dict[str, str]] | None) -> bool:
    """Return True if the EC2 ``Tags`` contain a Name with the SEC11 prefix."""
    if not tags:
        return False
    return any(t.get("Key") == "Name" and (t.get("Value") or "").startswith(SEC11_PREFIX) for t in tags)


def _cleanup_sec11_instances(ec2: Any) -> list[str]:
    """Terminate any leftover SEC11 EC2 instances and wait for the terminated state."""
    errors: list[str] = []
    instance_ids: list[str] = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=ISVTEST_TAG_FILTER):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                if instance.get("State", {}).get("Name") == "terminated":
                    continue
                if not _resource_has_sec11_name(instance.get("Tags")):
                    continue
                instance_ids.append(instance["InstanceId"])

    if not instance_ids:
        return errors

    try:
        ec2.terminate_instances(InstanceIds=instance_ids)
    except ClientError as e:
        errors.append(f"terminate instances {instance_ids}: {e}")
        return errors
    try:
        ec2.get_waiter("instance_terminated").wait(
            InstanceIds=instance_ids,
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},
        )
    except (ClientError, WaiterError) as e:
        # ``pending`` is a terminal failure for the InstanceTerminated
        # waiter; happens when a previous run died mid-launch. Volume
        # delete is retry-driven downstream, so log and move on.
        errors.append(f"wait terminated {instance_ids}: {e}")
    return errors


def _cleanup_sec11_volumes(ec2: Any) -> list[str]:
    """Delete leftover SEC11 EBS volumes (must run AFTER instance termination)."""
    errors: list[str] = []
    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=ISVTEST_TAG_FILTER):
        for volume in page.get("Volumes", []):
            if not _resource_has_sec11_name(volume.get("Tags")):
                continue
            volume_id = volume["VolumeId"]
            if not delete_with_retry(
                ec2.delete_volume,
                VolumeId=volume_id,
                resource_desc=f"volume {volume_id}",
            ):
                errors.append(f"delete volume {volume_id} failed")
    return errors


def _cleanup_sec11_vpcs(ec2: Any) -> list[str]:
    """Delete leftover SEC11 VPCs and their dependencies (security groups, subnets)."""
    errors: list[str] = []
    vpcs = ec2.describe_vpcs(Filters=ISVTEST_TAG_FILTER).get("Vpcs", [])
    for vpc in vpcs:
        if not _resource_has_sec11_name(vpc.get("Tags")):
            continue
        vpc_id = vpc["VpcId"]

        # Security groups (skip the default SG which cannot be deleted).
        sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("SecurityGroups", [])
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            if not delete_with_retry(
                ec2.delete_security_group,
                GroupId=sg["GroupId"],
                resource_desc=f"security group {sg['GroupId']}",
            ):
                errors.append(f"delete security group {sg['GroupId']} failed")

        # Subnets.
        subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("Subnets", [])
        for subnet in subnets:
            if not delete_with_retry(
                ec2.delete_subnet,
                SubnetId=subnet["SubnetId"],
                resource_desc=f"subnet {subnet['SubnetId']}",
            ):
                errors.append(f"delete subnet {subnet['SubnetId']} failed")

        if not delete_with_retry(
            ec2.delete_vpc,
            VpcId=vpc_id,
            resource_desc=f"VPC {vpc_id}",
        ):
            errors.append(f"delete VPC {vpc_id} failed")
    return errors


def _cleanup_sec11_kms(kms: Any) -> list[str]:
    """Schedule SEC11 KMS keys for deletion and remove their aliases."""
    errors: list[str] = []
    paginator = kms.get_paginator("list_aliases")
    for page in paginator.paginate():
        for alias in page.get("Aliases", []):
            alias_name = alias.get("AliasName", "")
            if not alias_name.startswith(SEC11_KMS_ALIAS_PREFIX):
                continue
            target_key = alias.get("TargetKeyId")
            try:
                kms.delete_alias(AliasName=alias_name)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "NotFoundException":
                    errors.append(f"delete kms alias {alias_name}: {e}")
            if target_key:
                try:
                    kms.schedule_key_deletion(KeyId=target_key, PendingWindowInDays=7)
                except ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    if code not in {"NotFoundException", "KMSInvalidStateException"}:
                        # KMSInvalidStateException = key is already pending deletion.
                        errors.append(f"schedule kms key {target_key} deletion: {e}")
    return errors


def _cleanup_sec11_buckets(s3: Any) -> list[str]:
    """Empty and delete leftover SEC11 S3 buckets."""
    errors: list[str] = []
    try:
        buckets = s3.list_buckets().get("Buckets", [])
    except ClientError as e:
        return [f"list_buckets: {e}"]

    for bucket in buckets:
        name = bucket.get("Name", "")
        if not name.startswith(SEC11_PREFIX):
            continue
        # Verified-ownership check: only delete buckets carrying our tag.
        try:
            tagging = s3.get_bucket_tagging(Bucket=name)
            tags = {t["Key"]: t["Value"] for t in tagging.get("TagSet", [])}
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchTagSet":
                tags = {}
            else:
                errors.append(f"get_bucket_tagging {name}: {e}")
                continue
        if tags.get("CreatedBy") != "isvtest":
            continue

        try:
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=name):
                to_delete = [
                    {"Key": v["Key"], "VersionId": v["VersionId"]}
                    for v in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])
                ]
                if to_delete:
                    s3.delete_objects(Bucket=name, Delete={"Objects": to_delete, "Quiet": True})
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in {"NoSuchBucket"}:
                errors.append(f"empty bucket {name}: {e}")

        if not delete_with_retry(s3.delete_bucket, Bucket=name, resource_desc=f"S3 bucket {name}"):
            errors.append(f"delete bucket {name} failed")
    return errors


def _sweep_iam_users(iam: Any) -> tuple[int, int, list[dict[str, Any]]]:
    """Sweep IAM users matching ``OWNED_USER_PREFIXES`` and tagged ``CreatedBy=isvtest``."""
    cleaned = 0
    skipped_unowned = 0
    failed_resources: list[dict[str, Any]] = []
    paginator = iam.get_paginator("list_users")
    for page in paginator.paginate():
        for user in page["Users"]:
            name = user["UserName"]
            if not name.startswith(OWNED_USER_PREFIXES):
                continue
            if not _user_has_isvtest_tag(iam, name):
                skipped_unowned += 1
                continue
            cleanup_errors = _cleanup_owned_user(iam, name)
            if cleanup_errors:
                failed_resources.append({"username": name, "errors": cleanup_errors})
            else:
                cleaned += 1
    return cleaned, skipped_unowned, failed_resources


@handle_aws_errors
def main() -> int:
    """Clean up leftover security test resources created by isvtest."""
    parser = argparse.ArgumentParser(description="Security test teardown")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "teardown",
    }

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        print(json.dumps(result, indent=2))
        return 0

    iam = boto3.client("iam", region_name=args.region)
    ec2 = boto3.client("ec2", region_name=args.region)
    kms = boto3.client("kms", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)

    sec11_errors: list[str] = []

    # Order matters: instances first (so volumes can be deleted), then
    # volumes, then VPCs (which need empty subnets/SGs).
    try:
        sec11_errors.extend(_cleanup_sec11_instances(ec2))
        sec11_errors.extend(_cleanup_sec11_volumes(ec2))
        sec11_errors.extend(_cleanup_sec11_vpcs(ec2))
        sec11_errors.extend(_cleanup_sec11_kms(kms))
        sec11_errors.extend(_cleanup_sec11_buckets(s3))
    except ClientError as e:
        sec11_errors.append(str(e))

    cleaned = 0
    skipped_unowned = 0
    failed_resources: list[dict[str, Any]] = []
    try:
        cleaned, skipped_unowned, failed_resources = _sweep_iam_users(iam)
    except ClientError as e:
        result["error"] = str(e)

    result["resources_cleaned"] = cleaned
    result["resources_skipped_unowned"] = skipped_unowned
    if sec11_errors:
        result["sec11_cleanup_errors"] = sec11_errors
    if failed_resources:
        result["resources_failed"] = failed_resources

    if failed_resources or sec11_errors:
        result["success"] = False
        existing_error = result.get("error", "")
        msgs: list[str] = []
        if failed_resources:
            msgs.append(
                f"Failed to clean up {len(failed_resources)} owned IAM user(s): "
                + "; ".join(f"{item['username']}: {', '.join(item['errors'])}" for item in failed_resources)
            )
        if sec11_errors:
            msgs.append("SEC11 sweep errors: " + "; ".join(sec11_errors))
        combined = "; ".join(msgs)
        result["error"] = f"{existing_error}; {combined}" if existing_error else combined
    elif "error" not in result:
        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
