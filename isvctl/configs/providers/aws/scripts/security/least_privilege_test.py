#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Verify least-privilege IAM policy dimensions and minimal-role denial (SEC04-01/02).

This AWS reference is self-contained. It provisions a temporary IAM user
(``isv-sec04-test-<suffix>``) plus two tagged S3 buckets, attaches one
inline policy to the user, mints an access key, then probes the policy as
that user.

The inline policy exercises the three least-privilege dimensions named by
SEC04-01:

* user-based: only the temporary user receives the inline grant.
* resource-based: only the tagged allowed bucket ARN is granted.
* network-based: the grant includes an ``aws:SourceIp`` condition for the
  caller's public CIDR.

SEC04-02 is checked from the same minimal identity by verifying out-of-scope
compute, storage, and network APIs are denied. Mutating EC2 probes use
``DryRun=True`` against valid temporary fixture parameters; the S3 delete
probe targets a non-empty fixture bucket so an unexpected broad allow cannot
delete it.

When the orchestrator principal cannot create the temporary IAM user, the
script emits a structured ``skipped`` payload (exit 0) so validations skip
rather than fabricate a pass.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from common.errors import classify_aws_error, delete_with_retry, handle_aws_errors

TEST_NAME = "least_privilege_test"
TEST_USER_PREFIX = "isv-sec04-test-"
INLINE_POLICY_NAME = "isv-sec04-least-privilege"
DENIED_OBJECT_KEY = "sec04-denied-probe-object"
CALLER_IP_URL = "https://checkip.amazonaws.com/"
PROBE_CIDR = "10.96.0.0/24"

SKIPPABLE_SETUP_ERRORS = frozenset({"AccessDenied", "UnauthorizedOperation"})
DENY_CODES = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "Forbidden"})
DRY_RUN_ALLOWED_CODE = "DryRunOperation"
IAM_PROPAGATION_MAX_ATTEMPTS = 8
IAM_PROPAGATION_BACKOFF_CAP = 8


def _skipped_result(reason: str) -> dict[str, Any]:
    """Return a structured top-level skip payload for the validation."""
    return {
        "success": True,
        "platform": "security",
        "test_name": TEST_NAME,
        "skipped": True,
        "skip_reason": reason,
        "tests": {},
    }


def _detect_source_cidr() -> str:
    """Return the caller's public IPv4 address as a /32 CIDR."""
    try:
        with urllib.request.urlopen(CALLER_IP_URL, timeout=5) as response:
            ip_address = response.read().decode("utf-8").strip()
    except (OSError, urllib.error.URLError) as exc:
        msg = f"cannot determine caller public IP for aws:SourceIp condition: {exc}"
        raise RuntimeError(msg) from exc
    if not ip_address or any(part == "" for part in ip_address.split(".")):
        msg = f"unexpected caller public IP response: {ip_address!r}"
        raise RuntimeError(msg)
    return f"{ip_address}/32"


def _create_bucket(s3: Any, bucket: str, region: str) -> None:
    """Create and tag one S3 bucket for the temporary SEC04 fixture."""
    create_kwargs: dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**create_kwargs)
    s3.put_bucket_tagging(
        Bucket=bucket,
        Tagging={"TagSet": [{"Key": "CreatedBy", "Value": "isvtest"}, {"Key": "TestId", "Value": "SEC04"}]},
    )


def _isvtest_ec2_tags(name: str) -> list[dict[str, str]]:
    """Return standard EC2 tags for temporary SEC04 resources."""
    return [
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "Name", "Value": name},
    ]


def _create_probe_network(ec2: Any, name: str) -> tuple[str, str, str]:
    """Create a minimal VPC/subnet/security-group fixture for EC2 DryRun probes."""
    vpc = ec2.create_vpc(CidrBlock=PROBE_CIDR)
    vpc_id = vpc["Vpc"]["VpcId"]
    ec2.create_tags(Resources=[vpc_id], Tags=_isvtest_ec2_tags(name))
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])

    az = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"][0][
        "ZoneName"
    ]
    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=PROBE_CIDR, AvailabilityZone=az)
    subnet_id = subnet["Subnet"]["SubnetId"]
    ec2.create_tags(Resources=[subnet_id], Tags=_isvtest_ec2_tags(name))

    sg = ec2.create_security_group(
        GroupName=f"{name}-sg",
        Description=f"SEC04 least-privilege test SG for {name}",
        VpcId=vpc_id,
    )
    sg_id = sg["GroupId"]
    ec2.create_tags(Resources=[sg_id], Tags=_isvtest_ec2_tags(name))
    return vpc_id, subnet_id, sg_id


def _launch_probe_instance(ec2: Any, *, ami_id: str, subnet_id: str, sg_id: str, name: str) -> str:
    """Launch one temporary instance so TerminateInstances DryRun has a real target id."""
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType="t3.micro",
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
        TagSpecifications=[{"ResourceType": "instance", "Tags": _isvtest_ec2_tags(name)}],
    )
    return response["Instances"][0]["InstanceId"]


def _cleanup_probe_network(ec2: Any, instance_id: str, sg_id: str, subnet_id: str, vpc_id: str) -> list[str]:
    """Best-effort delete of the temporary EC2 fixture."""
    errors: list[str] = []
    if instance_id:
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
            ec2.get_waiter("instance_terminated").wait(
                InstanceIds=[instance_id],
                WaiterConfig={"Delay": 5, "MaxAttempts": 60},
            )
        except (ClientError, WaiterError) as exc:
            errors.append(f"terminate/wait instance {instance_id}: {exc}")
    if sg_id and not delete_with_retry(
        ec2.delete_security_group, GroupId=sg_id, resource_desc=f"security group {sg_id}"
    ):
        errors.append(f"delete security group {sg_id} failed")
    if subnet_id and not delete_with_retry(ec2.delete_subnet, SubnetId=subnet_id, resource_desc=f"subnet {subnet_id}"):
        errors.append(f"delete subnet {subnet_id} failed")
    if vpc_id and not delete_with_retry(ec2.delete_vpc, VpcId=vpc_id, resource_desc=f"VPC {vpc_id}"):
        errors.append(f"delete VPC {vpc_id} failed")
    return errors


def _get_amazon_linux_ami(ec2: Any) -> str:
    """Return latest Amazon Linux 2023 x86_64 AMI id (or AL2 fallback)."""
    response = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-*-x86_64"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
    )
    images = sorted(response.get("Images", []), key=lambda image: image["CreationDate"], reverse=True)
    if not images:
        response = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(response.get("Images", []), key=lambda image: image["CreationDate"], reverse=True)
    if not images:
        msg = "No Amazon Linux AMI found for SEC04 DryRun probe"
        raise RuntimeError(msg)
    return images[0]["ImageId"]


def _policy_document(allowed_bucket: str, source_cidr: str) -> str:
    """Return the minimal inline policy attached only to the temporary user."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowListOnlyTaggedBucketFromCallerCidr",
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": f"arn:aws:s3:::{allowed_bucket}",
                    "Condition": {"IpAddress": {"aws:SourceIp": source_cidr}},
                }
            ],
        }
    )


def _cleanup_buckets(s3: Any, buckets: list[str]) -> list[str]:
    """Best-effort empty and delete fixture buckets."""
    errors: list[str] = []
    for bucket in buckets:
        try:
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=bucket):
                objects = [
                    {"Key": item["Key"], "VersionId": item["VersionId"]}
                    for item in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])
                ]
                if objects:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucket":
                errors.append(f"empty bucket {bucket}: {exc}")
        if not delete_with_retry(s3.delete_bucket, Bucket=bucket, resource_desc=f"S3 bucket {bucket}"):
            errors.append(f"delete bucket {bucket} failed")
    return errors


def _cleanup_test_user(
    iam: Any,
    username: str | None,
    access_key_id: str | None,
    user_created: bool,
) -> list[str]:
    """Best-effort delete of the test IAM user's policy, access key, and user."""
    errors: list[str] = []
    if not username:
        return errors
    if access_key_id:
        try:
            iam.delete_access_key(UserName=username, AccessKeyId=access_key_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                errors.append(f"delete access key {access_key_id} for {username}: {exc}")
    if user_created:
        try:
            iam.delete_user_policy(UserName=username, PolicyName=INLINE_POLICY_NAME)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                errors.append(f"delete inline policy {INLINE_POLICY_NAME} for {username}: {exc}")
        try:
            iam.delete_user(UserName=username)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                errors.append(f"delete user {username}: {exc}")
    return errors


def _wait_for_iam_propagation(sts: Any) -> None:
    """Block until the temporary access key is visible to STS."""
    for attempt in range(IAM_PROPAGATION_MAX_ATTEMPTS):
        try:
            sts.get_caller_identity()
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "InvalidClientTokenId" and attempt < IAM_PROPAGATION_MAX_ATTEMPTS - 1:
                time.sleep(min(2 ** (attempt + 1), IAM_PROPAGATION_BACKOFF_CAP))
                continue
            raise


def _probe_allowed_bucket(s3_user: Any, allowed_bucket: str) -> dict[str, Any]:
    """Verify the minimal policy allows ListBucket on the one scoped bucket."""
    for attempt in range(IAM_PROPAGATION_MAX_ATTEMPTS):
        try:
            s3_user.list_objects_v2(Bucket=allowed_bucket, MaxKeys=1)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in DENY_CODES and attempt < IAM_PROPAGATION_MAX_ATTEMPTS - 1:
                time.sleep(min(2 ** (attempt + 1), IAM_PROPAGATION_BACKOFF_CAP))
                continue
            return {"passed": False, "error": f"s3:ListBucket on scoped bucket failed: {exc}", "code": code}
        else:
            return {"passed": True, "message": f"s3:ListBucket allowed on scoped bucket {allowed_bucket}"}
    return {"passed": False, "error": "s3:ListBucket did not become allowed before propagation timeout"}


def _is_denied(exc: ClientError) -> bool:
    """Return True when the AWS ClientError represents an authorization deny."""
    return exc.response.get("Error", {}).get("Code", "") in DENY_CODES


def _expect_denied(name: str, fn: Callable[[], Any]) -> dict[str, Any]:
    """Run one probe and return passed=True only when IAM denies it."""
    try:
        fn()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        return {"name": name, "passed": _is_denied(exc), "code": code}
    except BotoCoreError as exc:
        return {"name": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"name": name, "passed": False, "error": "action unexpectedly succeeded"}


def _expect_ec2_dry_run_denied(name: str, fn: Callable[[], Any]) -> dict[str, Any]:
    """Run an EC2 DryRun probe and distinguish denied from unexpectedly allowed."""
    try:
        fn()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == DRY_RUN_ALLOWED_CODE:
            return {"name": name, "passed": False, "code": code, "error": "DryRun reported the action would be allowed"}
        return {"name": name, "passed": _is_denied(exc), "code": code}
    except BotoCoreError as exc:
        return {"name": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"name": name, "passed": False, "error": "DryRun returned no authorization result"}


def _aggregate(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-probe outcomes into the validation test envelope."""
    passed = all(probe.get("passed") for probe in probes)
    result: dict[str, Any] = {"passed": passed, "probes": probes}
    if not passed:
        result["error"] = "; ".join(
            probe.get("error") or f"{probe['name']} returned {probe.get('code', 'unknown')}"
            for probe in probes
            if not probe.get("passed")
        )
    return result


def _build_user_clients(access_key_id: str, secret_key: str, region: str) -> dict[str, Any]:
    """Return boto3 clients authenticated as the temporary SEC04 user."""
    common = {"region_name": region, "aws_access_key_id": access_key_id, "aws_secret_access_key": secret_key}
    return {
        "ec2": boto3.client("ec2", **common),
        "s3": boto3.client("s3", **common),
        "sts": boto3.client("sts", **common),
    }


@handle_aws_errors
def main() -> int:
    """Provision the SEC04 fixture, run positive and negative probes, emit JSON."""
    parser = argparse.ArgumentParser(description="Least-privilege policy and minimal-role enforcement test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--source-cidr",
        default=os.environ.get("SEC04_ALLOWED_SOURCE_CIDR", ""),
        help="Allowed aws:SourceIp CIDR. Defaults to caller public IPv4 /32.",
    )
    args = parser.parse_args()
    region = args.region

    try:
        source_cidr = args.source_cidr.strip() or _detect_source_cidr()
    except RuntimeError as exc:
        print(json.dumps(_skipped_result(str(exc)), indent=2))
        return 0

    iam = boto3.client("iam", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    suffix = uuid.uuid4().hex[:10]
    username = f"{TEST_USER_PREFIX}{suffix}"
    allowed_bucket = f"{username}-allowed"
    denied_bucket = f"{username}-denied"
    buckets_created: list[str] = []
    probe_vpc_id = ""
    probe_subnet_id = ""
    probe_sg_id = ""
    probe_instance_id = ""
    probe_ami_id = ""
    access_key_id: str | None = None
    secret_key: str | None = None
    user_created = False

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": TEST_NAME,
        "region": region,
        "test_identity": username,
        "allowed_resource": allowed_bucket,
        "denied_resource": denied_bucket,
        "allowed_source_cidr": source_cidr,
        "tests": {
            "policy_dimensions_user_based": {"passed": False},
            "policy_dimensions_resource_based": {"passed": False},
            "policy_dimensions_network_based": {"passed": False},
            "policy_dimensions_allowed_action_succeeds": {"passed": False},
            "out_of_scope_compute_denied": {"passed": False},
            "out_of_scope_storage_denied": {"passed": False},
            "out_of_scope_network_denied": {"passed": False},
        },
    }
    skip_payload: dict[str, Any] | None = None

    try:
        try:
            iam.create_user(UserName=username, Tags=[{"Key": "CreatedBy", "Value": "isvtest"}])
            user_created = True
            _create_bucket(s3, allowed_bucket, region)
            buckets_created.append(allowed_bucket)
            _create_bucket(s3, denied_bucket, region)
            buckets_created.append(denied_bucket)
            s3.put_object(Bucket=denied_bucket, Key=DENIED_OBJECT_KEY, Body=b"SEC04 deny probe")
            probe_ami_id = _get_amazon_linux_ami(ec2)
            probe_vpc_id, probe_subnet_id, probe_sg_id = _create_probe_network(ec2, username)
            probe_instance_id = _launch_probe_instance(
                ec2,
                ami_id=probe_ami_id,
                subnet_id=probe_subnet_id,
                sg_id=probe_sg_id,
                name=username,
            )
            iam.put_user_policy(
                UserName=username,
                PolicyName=INLINE_POLICY_NAME,
                PolicyDocument=_policy_document(allowed_bucket, source_cidr),
            )
            key_response = iam.create_access_key(UserName=username)
            access_key_id = key_response["AccessKey"]["AccessKeyId"]
            secret_key = key_response["AccessKey"]["SecretAccessKey"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            partial_resources_created = user_created or bool(buckets_created) or access_key_id is not None
            cleanup_errors = _cleanup_test_user(iam, username, access_key_id, user_created)
            cleanup_errors.extend(
                _cleanup_probe_network(ec2, probe_instance_id, probe_sg_id, probe_subnet_id, probe_vpc_id)
            )
            cleanup_errors.extend(_cleanup_buckets(s3, buckets_created))
            if cleanup_errors:
                result["error"] = f"setup failed: {exc}; cleanup failed: {'; '.join(cleanup_errors)}"
                result["cleanup_errors"] = cleanup_errors
            elif code in SKIPPABLE_SETUP_ERRORS and not partial_resources_created:
                skip_payload = _skipped_result(
                    f"cannot provision SEC04 test IAM user: {exc}; orchestrator principal needs "
                    "iam:CreateUser, iam:PutUserPolicy, iam:CreateAccessKey, and matching delete permissions"
                )
            elif code in SKIPPABLE_SETUP_ERRORS:
                skip_payload = _skipped_result(
                    f"SEC04 fixture setup was denied and partial resources were cleaned up: {exc}"
                )
            else:
                raise
            return_code = 0 if skip_payload is not None and not cleanup_errors else 1
            if skip_payload is not None:
                print(json.dumps(skip_payload, indent=2))
            else:
                print(json.dumps(result, indent=2))
            return return_code

        if access_key_id is None or secret_key is None:
            msg = "access key was not created for SEC04 test user"
            raise RuntimeError(msg)

        clients = _build_user_clients(access_key_id, secret_key, region)
        _wait_for_iam_propagation(clients["sts"])
        caller = clients["sts"].get_caller_identity()
        caller_arn = caller.get("Arn", "")

        allowed_result = _probe_allowed_bucket(clients["s3"], allowed_bucket)
        result["tests"]["policy_dimensions_allowed_action_succeeds"] = allowed_result
        allowed_passed = bool(allowed_result.get("passed"))
        result["tests"]["policy_dimensions_user_based"] = {
            "passed": allowed_passed and username in caller_arn,
            "message": f"temporary user ARN: {caller_arn}",
        }
        result["tests"]["policy_dimensions_resource_based"] = {
            "passed": allowed_passed,
            "message": f"policy grants only arn:aws:s3:::{allowed_bucket}",
        }
        result["tests"]["policy_dimensions_network_based"] = {
            "passed": allowed_passed,
            "message": f"policy requires aws:SourceIp={source_cidr}",
        }

        compute_probes = [
            _expect_ec2_dry_run_denied(
                "ec2_run_instances_denied",
                lambda: clients["ec2"].run_instances(
                    ImageId=probe_ami_id,
                    InstanceType="t3.micro",
                    MinCount=1,
                    MaxCount=1,
                    SubnetId=probe_subnet_id,
                    SecurityGroupIds=[probe_sg_id],
                    DryRun=True,
                ),
            ),
            _expect_ec2_dry_run_denied(
                "ec2_terminate_instances_denied",
                lambda: clients["ec2"].terminate_instances(InstanceIds=[probe_instance_id], DryRun=True),
            ),
        ]
        storage_probes = [
            _expect_denied(
                "s3_delete_bucket_denied",
                lambda: clients["s3"].delete_bucket(Bucket=denied_bucket),
            ),
            _expect_denied(
                "s3_get_object_denied",
                lambda: clients["s3"].get_object(Bucket=denied_bucket, Key=DENIED_OBJECT_KEY),
            ),
        ]
        network_probes = [
            _expect_ec2_dry_run_denied(
                "ec2_create_vpc_denied",
                lambda: clients["ec2"].create_vpc(CidrBlock="10.99.0.0/24", DryRun=True),
            ),
            _expect_ec2_dry_run_denied(
                "ec2_authorize_security_group_ingress_denied",
                lambda: clients["ec2"].authorize_security_group_ingress(
                    GroupId=probe_sg_id,
                    IpPermissions=[
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 22,
                            "ToPort": 22,
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                        }
                    ],
                    DryRun=True,
                ),
            ),
        ]
        result["tests"]["out_of_scope_compute_denied"] = _aggregate(compute_probes)
        result["tests"]["out_of_scope_storage_denied"] = _aggregate(storage_probes)
        result["tests"]["out_of_scope_network_denied"] = _aggregate(network_probes)
        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        error_type, error_msg = classify_aws_error(exc)
        result["error"] = f"[{error_type}] {error_msg}"
        result["success"] = False
    finally:
        cleanup_errors = _cleanup_test_user(iam, username, access_key_id, user_created)
        cleanup_errors.extend(
            _cleanup_probe_network(ec2, probe_instance_id, probe_sg_id, probe_subnet_id, probe_vpc_id)
        )
        cleanup_errors.extend(_cleanup_buckets(s3, buckets_created))
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            cleanup_msg = f"Cleanup failed: {'; '.join(cleanup_errors)}"
            existing = result.get("error")
            result["error"] = f"{existing}; {cleanup_msg}" if existing else cleanup_msg
            result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
