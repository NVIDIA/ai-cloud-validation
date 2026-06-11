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

"""Verify hard tenant isolation across network/data/compute/storage (SEC11-01).

Self-contained AWS reference. Provisions two ephemeral "tenants" in a try /
finally block, where each tenant = (VPC + subnet + SG, IAM user with an
inline policy scoped to its own resources, KMS CMK, S3 bucket, t3.micro EC2
instance, 1 GiB EBS volume), then runs four cross-tenant negative probes
from tenant A's IAM creds against tenant B's resources, asserting each is
denied:

  * network_isolated: no VPC peering between A and B and no route from
    A's route tables targeting B's CIDR (orchestrator describe; mirrors
    bmc_isolation_test.py's config-plane check).
  * data_isolated: tenant A's IAM principal denied ``kms:Encrypt`` on
    tenant B's CMK and ``s3:GetObject`` on tenant B's bucket.
  * compute_isolated: tenant A denied ``ec2:StopInstances`` (DryRun) and
    ``ssm:StartSession`` against tenant B's instance.
  * storage_isolated: tenant A denied ``ec2:CreateSnapshot`` (DryRun) and
    ``ec2:AttachVolume`` (DryRun) against tenant B's volume.

Per-tenant resources share an ``isv-sec11-test-<suffix>`` prefix and the
``CreatedBy=isvtest`` tag so the security teardown sweep can clean up
anything this script leaks on a hard crash.

Physical isolation (bare-metal) and IB switch-fabric isolation are out of
scope for this test (covered by SDN04-04/05).

Usage:
    python tenant_isolation_test.py --region us-west-2

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "tenant_isolation_test",
    "tenant_a_id": "isv-sec11-test-aaaa1111",
    "tenant_b_id": "isv-sec11-test-bbbb2222",
    "tests": {
      "network_isolated": {"passed": true, "message": "..."},
      "data_isolated":    {"passed": true, "probes": [...]},
      "compute_isolated": {"passed": true, "probes": [...]},
      "storage_isolated": {"passed": true, "probes": [...]}
    }
  }
"""

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from common.errors import classify_aws_error, delete_with_retry, handle_aws_errors

TEST_NAME = "tenant_isolation_test"
TENANT_PREFIX = "isv-sec11-test-"
INLINE_POLICY_NAME = "isv-sec11-tenant-scope"
TENANT_A_CIDR = "10.94.0.0/24"
TENANT_B_CIDR = "10.95.0.0/24"
INSTANCE_TYPE = "t3.micro"
VOLUME_SIZE_GIB = 1

# AWS error codes from setup that signal the orchestrator principal cannot
# provision the test fixture; surface as a structured skip rather than a
# SEC11-01 failure.
SKIPPABLE_SETUP_ERRORS = frozenset({"AccessDenied", "UnauthorizedOperation"})

# IAM is eventually consistent: a freshly created access key may take
# 15-30s to propagate. Mirror short_lived_credentials_test.py's pattern.
IAM_PROPAGATION_MAX_ATTEMPTS = 8
IAM_PROPAGATION_BACKOFF_CAP = 8

# Error codes that mean the probe was correctly denied. UnauthorizedOperation
# is what EC2 returns for DryRun calls when IAM denies; AccessDenied is the
# generic deny for KMS, S3, SSM, IAM-scoped actions.
DENY_CODES = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "Forbidden"})

# Error code returned by EC2 when DryRun=True succeeds (i.e. IAM allowed it
# but the action was not performed). Treated as "probe was NOT denied", so
# the cross-tenant probe FAILS.
DRY_RUN_ALLOWED_CODE = "DryRunOperation"

# TGW VPC-attachment states that still represent a live cross-VPC bridge.
# Anything outside this set (deleted, deleting, failed, ...) is a stale
# attachment that can't carry traffic.
LIVE_TGW_STATES = frozenset({"available", "pending", "modifying", "initiating", "initiatingRequest", "rollingBack"})


@dataclass
class Tenant:
    """Per-tenant resources created during setup, used during probes and teardown."""

    suffix: str
    cidr: str
    vpc_id: str = ""
    subnet_id: str = ""
    availability_zone: str = ""
    sg_id: str = ""
    iam_user_arn: str = ""
    access_key_id: str = ""
    secret_key: str = ""
    kms_key_id: str = ""
    kms_key_arn: str = ""
    s3_bucket: str = ""
    instance_id: str = ""
    volume_id: str = ""
    created: dict[str, bool] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Return the canonical ``isv-sec11-test-<suffix>`` identifier."""
        return f"{TENANT_PREFIX}{self.suffix}"


# -- Setup helpers --------------------------------------------------------


def _scoped_policy_document(tenant: Tenant) -> str:
    """IAM policy granting tenant A's user actions only on its OWN resources.

    Cross-tenant probes against tenant B's resources fall outside this
    allow-list and hit IAM's default deny -- which is exactly what
    SEC11-01 wants to see. The test does not actually require A to be
    able to act on A's own resources; granting these actions is
    sanity-only so the inline policy is a realistic tenant scope rather
    than an empty deny-all.
    """
    instance_arn = f"arn:aws:ec2:*:*:instance/{tenant.instance_id}"
    volume_arn = f"arn:aws:ec2:*:*:volume/{tenant.volume_id}"
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowOwnKms",
                    "Effect": "Allow",
                    "Action": ["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey"],
                    "Resource": [tenant.kms_key_arn],
                },
                {
                    "Sid": "AllowOwnBucket",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                    "Resource": [
                        f"arn:aws:s3:::{tenant.s3_bucket}",
                        f"arn:aws:s3:::{tenant.s3_bucket}/*",
                    ],
                },
                {
                    "Sid": "AllowOwnInstance",
                    "Effect": "Allow",
                    "Action": ["ec2:StopInstances", "ec2:StartInstances", "ssm:StartSession"],
                    "Resource": [instance_arn],
                },
                {
                    "Sid": "AllowOwnVolumeAndAttach",
                    "Effect": "Allow",
                    "Action": ["ec2:CreateSnapshot", "ec2:AttachVolume", "ec2:DetachVolume"],
                    "Resource": [volume_arn, instance_arn],
                },
            ],
        }
    )


def _isvtest_tags(name: str) -> list[dict[str, str]]:
    """Standard ``CreatedBy=isvtest`` + ``Name`` tag pair for EC2 resources."""
    return [
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "Name", "Value": name},
    ]


def _create_vpc_and_subnet(ec2: Any, tenant: Tenant) -> None:
    """Create VPC, subnet, default SG; populate ``tenant`` in place."""
    vpc = ec2.create_vpc(CidrBlock=tenant.cidr)
    tenant.vpc_id = vpc["Vpc"]["VpcId"]
    tenant.created["vpc"] = True
    ec2.create_tags(Resources=[tenant.vpc_id], Tags=_isvtest_tags(tenant.name))
    ec2.get_waiter("vpc_available").wait(VpcIds=[tenant.vpc_id])

    az = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"][0][
        "ZoneName"
    ]
    tenant.availability_zone = az
    subnet = ec2.create_subnet(VpcId=tenant.vpc_id, CidrBlock=tenant.cidr, AvailabilityZone=az)
    tenant.subnet_id = subnet["Subnet"]["SubnetId"]
    tenant.created["subnet"] = True
    ec2.create_tags(Resources=[tenant.subnet_id], Tags=_isvtest_tags(tenant.name))

    sg = ec2.create_security_group(
        GroupName=f"{tenant.name}-sg",
        Description=f"SEC11-01 tenant isolation test SG for {tenant.name}",
        VpcId=tenant.vpc_id,
    )
    tenant.sg_id = sg["GroupId"]
    tenant.created["sg"] = True
    ec2.create_tags(Resources=[tenant.sg_id], Tags=_isvtest_tags(tenant.name))


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
    images = sorted(response.get("Images", []), key=lambda x: x["CreationDate"], reverse=True)
    if not images:
        # Fallback to AL2 if AL2023 unavailable in this partition.
        response = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(response.get("Images", []), key=lambda x: x["CreationDate"], reverse=True)
    if not images:
        msg = "No Amazon Linux AMI found for tenant fixture"
        raise RuntimeError(msg)
    return images[0]["ImageId"]


def _launch_instance(ec2: Any, tenant: Tenant, ami_id: str) -> None:
    """Launch a single t3.micro into tenant's subnet (no key pair, no user data)."""
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=INSTANCE_TYPE,
        MinCount=1,
        MaxCount=1,
        SubnetId=tenant.subnet_id,
        SecurityGroupIds=[tenant.sg_id],
        TagSpecifications=[{"ResourceType": "instance", "Tags": _isvtest_tags(tenant.name)}],
    )
    tenant.instance_id = response["Instances"][0]["InstanceId"]
    tenant.created["instance"] = True


def _create_volume(ec2: Any, tenant: Tenant) -> None:
    """Create a 1 GiB gp3 EBS volume in the tenant's AZ."""
    response = ec2.create_volume(
        AvailabilityZone=tenant.availability_zone,
        Size=VOLUME_SIZE_GIB,
        VolumeType="gp3",
        TagSpecifications=[{"ResourceType": "volume", "Tags": _isvtest_tags(tenant.name)}],
    )
    tenant.volume_id = response["VolumeId"]
    tenant.created["volume"] = True
    ec2.get_waiter("volume_available").wait(
        VolumeIds=[tenant.volume_id],
        WaiterConfig={"Delay": 2, "MaxAttempts": 30},
    )


def _create_kms_key(kms: Any, tenant: Tenant) -> None:
    """Create a tagged symmetric customer-managed KMS key for the tenant."""
    response = kms.create_key(
        Description=f"SEC11-01 tenant isolation test key for {tenant.name}",
        KeyUsage="ENCRYPT_DECRYPT",
        Origin="AWS_KMS",
        Tags=[
            {"TagKey": "CreatedBy", "TagValue": "isvtest"},
            {"TagKey": "Name", "TagValue": tenant.name},
        ],
    )
    tenant.kms_key_id = response["KeyMetadata"]["KeyId"]
    tenant.kms_key_arn = response["KeyMetadata"]["Arn"]
    tenant.created["kms_key"] = True
    # Attach a friendly alias so teardown's prefix sweep can find orphaned
    # keys without scanning the whole region.
    kms.create_alias(AliasName=f"alias/{tenant.name}", TargetKeyId=tenant.kms_key_id)
    tenant.created["kms_alias"] = True


def _create_s3_bucket(s3: Any, tenant: Tenant, region: str) -> None:
    """Create a tagged S3 bucket. us-east-1 must NOT pass LocationConstraint."""
    bucket = f"{tenant.name}-{uuid.uuid4().hex[:8]}"
    create_kwargs: dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**create_kwargs)
    tenant.s3_bucket = bucket
    tenant.created["s3"] = True
    s3.put_bucket_tagging(
        Bucket=bucket,
        Tagging={"TagSet": [{"Key": "CreatedBy", "Value": "isvtest"}, {"Key": "Name", "Value": tenant.name}]},
    )


def _create_iam_user(iam: Any, tenant: Tenant) -> None:
    """Create the tenant's IAM user with a scoped inline policy + access key."""
    response = iam.create_user(
        UserName=tenant.name,
        Tags=[{"Key": "CreatedBy", "Value": "isvtest"}, {"Key": "Tenant", "Value": tenant.name}],
    )
    tenant.iam_user_arn = response["User"]["Arn"]
    tenant.created["iam_user"] = True

    iam.put_user_policy(
        UserName=tenant.name,
        PolicyName=INLINE_POLICY_NAME,
        PolicyDocument=_scoped_policy_document(tenant),
    )
    tenant.created["iam_policy"] = True

    key_response = iam.create_access_key(UserName=tenant.name)
    tenant.access_key_id = key_response["AccessKey"]["AccessKeyId"]
    tenant.secret_key = key_response["AccessKey"]["SecretAccessKey"]
    tenant.created["iam_access_key"] = True


def _provision_tenant(
    *,
    ec2: Any,
    iam: Any,
    kms: Any,
    s3: Any,
    region: str,
    tenant: Tenant,
    ami_id: str,
) -> Tenant:
    """Provision the full per-tenant fixture into a caller-owned resource ledger."""
    _create_vpc_and_subnet(ec2, tenant)
    _create_s3_bucket(s3, tenant, region)
    _create_kms_key(kms, tenant)
    _launch_instance(ec2, tenant, ami_id)
    _create_volume(ec2, tenant)
    # IAM user must be created last: its inline policy ARNs reference the
    # KMS key, S3 bucket, instance, and volume created above.
    _create_iam_user(iam, tenant)
    return tenant


# -- Teardown -------------------------------------------------------------


def _teardown_tenant(
    *,
    ec2: Any,
    iam: Any,
    kms: Any,
    s3: Any,
    tenant: Tenant,
) -> list[str]:
    """Best-effort teardown of every resource the setup helpers created.

    Each step is independent so a transient failure on one resource does
    not leak the rest.
    """
    errors: list[str] = []

    if tenant.created.get("instance") and tenant.instance_id:
        # NB: catch WaiterError too -- the InstanceTerminated waiter treats
        # "pending" as a terminal failure, which fires when setup raised
        # before the instance reached running. Letting it propagate would
        # mask the original error (and skip the rest of cleanup); the
        # safety-net teardown step picks up any leftover instance.
        try:
            ec2.terminate_instances(InstanceIds=[tenant.instance_id])
        except ClientError as e:
            errors.append(f"terminate instance {tenant.instance_id}: {e}")
        else:
            try:
                ec2.get_waiter("instance_terminated").wait(
                    InstanceIds=[tenant.instance_id],
                    WaiterConfig={"Delay": 5, "MaxAttempts": 60},
                )
            except (ClientError, WaiterError) as e:
                errors.append(f"wait terminated {tenant.instance_id}: {e}")

    if tenant.created.get("volume") and tenant.volume_id:
        if not delete_with_retry(
            ec2.delete_volume,
            VolumeId=tenant.volume_id,
            resource_desc=f"volume {tenant.volume_id}",
        ):
            errors.append(f"delete volume {tenant.volume_id} failed")

    if tenant.created.get("iam_access_key") and tenant.access_key_id:
        try:
            iam.delete_access_key(UserName=tenant.name, AccessKeyId=tenant.access_key_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
                errors.append(f"delete access key for {tenant.name}: {e}")

    if tenant.created.get("iam_policy"):
        try:
            iam.delete_user_policy(UserName=tenant.name, PolicyName=INLINE_POLICY_NAME)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
                errors.append(f"delete inline policy for {tenant.name}: {e}")

    if tenant.created.get("iam_user"):
        try:
            iam.delete_user(UserName=tenant.name)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
                errors.append(f"delete user {tenant.name}: {e}")

    if tenant.created.get("kms_alias"):
        try:
            kms.delete_alias(AliasName=f"alias/{tenant.name}")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NotFoundException":
                errors.append(f"delete kms alias {tenant.name}: {e}")

    if tenant.created.get("kms_key") and tenant.kms_key_id:
        try:
            kms.schedule_key_deletion(KeyId=tenant.kms_key_id, PendingWindowInDays=7)
        except ClientError as e:
            errors.append(f"schedule kms key deletion for {tenant.kms_key_id}: {e}")

    if tenant.created.get("s3") and tenant.s3_bucket:
        errors.extend(_empty_and_delete_bucket(s3, tenant.s3_bucket))

    if tenant.created.get("sg") and tenant.sg_id:
        if not delete_with_retry(
            ec2.delete_security_group,
            GroupId=tenant.sg_id,
            resource_desc=f"security group {tenant.sg_id}",
        ):
            errors.append(f"delete security group {tenant.sg_id} failed")

    if tenant.created.get("subnet") and tenant.subnet_id:
        if not delete_with_retry(
            ec2.delete_subnet,
            SubnetId=tenant.subnet_id,
            resource_desc=f"subnet {tenant.subnet_id}",
        ):
            errors.append(f"delete subnet {tenant.subnet_id} failed")

    if tenant.created.get("vpc") and tenant.vpc_id:
        if not delete_with_retry(
            ec2.delete_vpc,
            VpcId=tenant.vpc_id,
            resource_desc=f"VPC {tenant.vpc_id}",
        ):
            errors.append(f"delete VPC {tenant.vpc_id} failed")

    return errors


def _empty_and_delete_bucket(s3: Any, bucket: str) -> list[str]:
    """Empty an S3 bucket (objects + versions + delete markers) then delete it."""
    errors: list[str] = []
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            to_delete = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])
            ]
            if to_delete:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete, "Quiet": True})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in {"NoSuchBucket"}:
            errors.append(f"empty bucket {bucket}: {e}")

    if not delete_with_retry(s3.delete_bucket, Bucket=bucket, resource_desc=f"S3 bucket {bucket}"):
        errors.append(f"delete bucket {bucket} failed")
    return errors


# -- Probe helpers --------------------------------------------------------


def _is_denied(exc: ClientError) -> bool:
    """Return True when the AWS ClientError represents an authorization deny."""
    code = exc.response.get("Error", {}).get("Code", "")
    return code in DENY_CODES


def _classify_dry_run(exc: ClientError) -> str:
    """Classify an EC2 DryRun ClientError as ``denied``, ``allowed``, or ``other``."""
    code = exc.response.get("Error", {}).get("Code", "")
    if code in DENY_CODES:
        return "denied"
    if code == DRY_RUN_ALLOWED_CODE:
        return "allowed"
    return "other"


def _build_probe_result(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-probe results into the ``passed`` / ``probes`` / ``error`` envelope."""
    passed = all(p["passed"] for p in probes)
    result: dict[str, Any] = {"passed": passed, "probes": probes}
    if not passed:
        result["error"] = "; ".join(p.get("error", p.get("code", "")) for p in probes if not p["passed"])
    return result


def _wait_for_iam_propagation(sts_a: Any) -> None:
    """Block until tenant A's STS client can call GetCallerIdentity (or give up)."""
    for attempt in range(IAM_PROPAGATION_MAX_ATTEMPTS):
        try:
            sts_a.get_caller_identity()
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "InvalidClientTokenId" and attempt < IAM_PROPAGATION_MAX_ATTEMPTS - 1:
                time.sleep(min(2 ** (attempt + 1), IAM_PROPAGATION_BACKOFF_CAP))
                continue
            raise


def _build_tenant_clients(tenant: Tenant, region: str) -> dict[str, Any]:
    """Return boto3 clients authenticated as ``tenant`` (used for negative probes)."""
    common = {
        "region_name": region,
        "aws_access_key_id": tenant.access_key_id,
        "aws_secret_access_key": tenant.secret_key,
    }
    return {
        "ec2": boto3.client("ec2", **common),
        "kms": boto3.client("kms", **common),
        "s3": boto3.client("s3", **common),
        "ssm": boto3.client("ssm", **common),
        "sts": boto3.client("sts", **common),
    }


# -- Probes ---------------------------------------------------------------


def _probe_network_isolation(orchestrator_ec2: Any, tenant_a: Tenant, tenant_b: Tenant) -> dict[str, Any]:
    """Verify no peering, TGW attachment, or shared route between tenant A and B.

    Config-plane check (mirrors bmc_isolation_test.py): two freshly created
    VPCs in AWS are unreachable to each other unless cross-VPC plumbing is
    wired in. We assert none of the three common mechanisms exists --
    VPC peering, Transit Gateway attachment binding both VPCs, or a route
    in tenant A's route tables targeting tenant B's CIDR.
    """
    peerings = orchestrator_ec2.describe_vpc_peering_connections(
        Filters=[
            {"Name": "accepter-vpc-info.vpc-id", "Values": [tenant_a.vpc_id, tenant_b.vpc_id]},
            {"Name": "requester-vpc-info.vpc-id", "Values": [tenant_a.vpc_id, tenant_b.vpc_id]},
        ]
    ).get("VpcPeeringConnections", [])
    cross_peerings = [
        p for p in peerings if p.get("Status", {}).get("Code") in {"active", "pending-acceptance", "provisioning"}
    ]
    if cross_peerings:
        return {
            "passed": False,
            "error": f"VPC peering exists between tenant A ({tenant_a.vpc_id}) and tenant B ({tenant_b.vpc_id})",
        }

    # Transit Gateway: a TGW attached to BOTH VPCs is a separate teleport
    # mechanism that would route around the peering check above. We
    # describe attachments for either VPC and flag any TGW that appears
    # for both.
    tgw_attachments = orchestrator_ec2.describe_transit_gateway_vpc_attachments(
        Filters=[{"Name": "vpc-id", "Values": [tenant_a.vpc_id, tenant_b.vpc_id]}]
    ).get("TransitGatewayVpcAttachments", [])
    by_tgw: dict[str, set[str]] = {}
    for att in tgw_attachments:
        if att.get("State") not in LIVE_TGW_STATES:
            continue
        tgw_id = att.get("TransitGatewayId")
        vpc_id = att.get("VpcId")
        if not tgw_id or not vpc_id:
            continue
        by_tgw.setdefault(tgw_id, set()).add(vpc_id)
    shared_tgws = [tgw_id for tgw_id, vpcs in by_tgw.items() if {tenant_a.vpc_id, tenant_b.vpc_id}.issubset(vpcs)]
    if shared_tgws:
        return {
            "passed": False,
            "error": (
                f"Transit Gateway {shared_tgws[0]} bridges tenant A ({tenant_a.vpc_id}) "
                f"and tenant B ({tenant_b.vpc_id})"
            ),
        }

    tenant_b_cidrs = {tenant_b.cidr}
    a_route_tables = orchestrator_ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [tenant_a.vpc_id]}]
    ).get("RouteTables", [])
    for rt in a_route_tables:
        for route in rt.get("Routes", []):
            if route.get("DestinationCidrBlock") in tenant_b_cidrs:
                return {
                    "passed": False,
                    "error": (
                        f"Route to tenant B CIDR {route['DestinationCidrBlock']} found in "
                        f"{rt['RouteTableId']} for tenant A VPC {tenant_a.vpc_id}"
                    ),
                }

    return {
        "passed": True,
        "message": (
            f"No peering, transit gateway, or shared route between tenant A VPC {tenant_a.vpc_id} "
            f"({tenant_a.cidr}) and tenant B VPC {tenant_b.vpc_id} ({tenant_b.cidr})"
        ),
    }


def _probe_data_isolation(clients_a: dict[str, Any], tenant_b: Tenant) -> dict[str, Any]:
    """Tenant A's IAM principal must be denied kms:Encrypt on B's CMK and s3:GetObject on B's bucket."""
    probes: list[dict[str, Any]] = []

    try:
        clients_a["kms"].encrypt(KeyId=tenant_b.kms_key_arn, Plaintext=b"sec11-cross-tenant")
    except ClientError as exc:
        probes.append(
            {
                "name": "kms_encrypt_denied",
                "passed": _is_denied(exc),
                "code": exc.response.get("Error", {}).get("Code", ""),
            }
        )
    else:
        probes.append({"name": "kms_encrypt_denied", "passed": False, "error": "kms:Encrypt unexpectedly succeeded"})

    try:
        clients_a["s3"].get_object(Bucket=tenant_b.s3_bucket, Key="probe-key")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # NoSuchKey would mean ListBucket was implicitly granted (it is not),
        # so any "not denied" outcome here is a real failure.
        probes.append(
            {
                "name": "s3_get_object_denied",
                "passed": _is_denied(exc),
                "code": code,
            }
        )
    else:
        probes.append({"name": "s3_get_object_denied", "passed": False, "error": "s3:GetObject unexpectedly succeeded"})

    return _build_probe_result(probes)


def _probe_compute_isolation(clients_a: dict[str, Any], tenant_b: Tenant) -> dict[str, Any]:
    """Tenant A must be denied ec2:StopInstances (DryRun) and ssm:StartSession against B's instance."""
    probes: list[dict[str, Any]] = []

    try:
        clients_a["ec2"].stop_instances(InstanceIds=[tenant_b.instance_id], DryRun=True)
    except ClientError as exc:
        verdict = _classify_dry_run(exc)
        probes.append(
            {
                "name": "ec2_stop_instances_denied",
                "passed": verdict == "denied",
                "code": exc.response.get("Error", {}).get("Code", ""),
            }
        )
    else:
        # No exception means EC2 silently returned without DryRunOperation,
        # which AWS does not do for DryRun=True; treat defensively as fail.
        probes.append(
            {
                "name": "ec2_stop_instances_denied",
                "passed": False,
                "error": "ec2:StopInstances DryRun returned no error (unexpected)",
            }
        )

    try:
        clients_a["ssm"].start_session(Target=tenant_b.instance_id)
    except ClientError as exc:
        probes.append(
            {
                "name": "ssm_start_session_denied",
                "passed": _is_denied(exc),
                "code": exc.response.get("Error", {}).get("Code", ""),
            }
        )
    else:
        probes.append(
            {"name": "ssm_start_session_denied", "passed": False, "error": "ssm:StartSession unexpectedly succeeded"}
        )

    return _build_probe_result(probes)


def _probe_storage_isolation(clients_a: dict[str, Any], tenant_a: Tenant, tenant_b: Tenant) -> dict[str, Any]:
    """Tenant A must be denied ec2:CreateSnapshot (DryRun) and ec2:AttachVolume (DryRun) against B's volume."""
    probes: list[dict[str, Any]] = []

    try:
        clients_a["ec2"].create_snapshot(VolumeId=tenant_b.volume_id, DryRun=True)
    except ClientError as exc:
        verdict = _classify_dry_run(exc)
        probes.append(
            {
                "name": "ec2_create_snapshot_denied",
                "passed": verdict == "denied",
                "code": exc.response.get("Error", {}).get("Code", ""),
            }
        )
    else:
        probes.append(
            {
                "name": "ec2_create_snapshot_denied",
                "passed": False,
                "error": "ec2:CreateSnapshot DryRun returned no error (unexpected)",
            }
        )

    # AttachVolume requires (volume, instance) ARN pair; we use tenant A's
    # own instance as the attach target so the only deny axis is the
    # foreign volume ARN.
    try:
        clients_a["ec2"].attach_volume(
            VolumeId=tenant_b.volume_id,
            InstanceId=tenant_a.instance_id,
            Device="/dev/sdf",
            DryRun=True,
        )
    except ClientError as exc:
        verdict = _classify_dry_run(exc)
        probes.append(
            {
                "name": "ec2_attach_volume_denied",
                "passed": verdict == "denied",
                "code": exc.response.get("Error", {}).get("Code", ""),
            }
        )
    else:
        probes.append(
            {
                "name": "ec2_attach_volume_denied",
                "passed": False,
                "error": "ec2:AttachVolume DryRun returned no error (unexpected)",
            }
        )

    return _build_probe_result(probes)


# -- Main -----------------------------------------------------------------


def _skipped_result(reason: str) -> dict[str, Any]:
    """Return a structured top-level skip payload (validation will skip rather than fabricate a pass)."""
    return {
        "success": True,
        "platform": "security",
        "test_name": TEST_NAME,
        "skipped": True,
        "skip_reason": reason,
        "tests": {},
    }


@handle_aws_errors
def main() -> int:
    """Provision two tenants, run cross-tenant negative probes, emit JSON, clean up."""
    parser = argparse.ArgumentParser(description="Tenant isolation test (SEC11-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()
    region = args.region

    ec2 = boto3.client("ec2", region_name=region)
    iam = boto3.client("iam", region_name=region)
    kms = boto3.client("kms", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    suffix_a = uuid.uuid4().hex[:8]
    suffix_b = uuid.uuid4().hex[:8]

    tenant_a: Tenant | None = None
    tenant_b: Tenant | None = None

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": TEST_NAME,
        "region": region,
        "tenant_a_id": "",
        "tenant_b_id": "",
        "tests": {
            "network_isolated": {"passed": False},
            "data_isolated": {"passed": False},
            "compute_isolated": {"passed": False},
            "storage_isolated": {"passed": False},
        },
    }

    skip_payload: dict[str, Any] | None = None

    try:
        try:
            ami_id = _get_amazon_linux_ami(ec2)
            tenant_a = Tenant(suffix=suffix_a, cidr=TENANT_A_CIDR)
            tenant_a = _provision_tenant(
                ec2=ec2,
                iam=iam,
                kms=kms,
                s3=s3,
                region=region,
                tenant=tenant_a,
                ami_id=ami_id,
            )
            tenant_b = Tenant(suffix=suffix_b, cidr=TENANT_B_CIDR)
            tenant_b = _provision_tenant(
                ec2=ec2,
                iam=iam,
                kms=kms,
                s3=s3,
                region=region,
                tenant=tenant_b,
                ami_id=ami_id,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            partial_resources_created = any(
                tenant is not None and any(tenant.created.values()) for tenant in (tenant_a, tenant_b)
            )
            if code in SKIPPABLE_SETUP_ERRORS and not partial_resources_created:
                # Pure-permission denial with NO partial state -- emit a skip
                # so the validation pytest.skips rather than fabricating a pass.
                skip_payload = _skipped_result(
                    f"cannot provision SEC11-01 tenant fixture: {exc}; "
                    "orchestrator principal needs ec2/iam/kms/s3 create+delete perms "
                    "(see script docstring)"
                )
            else:
                raise

        if tenant_a is not None and tenant_b is not None:
            result["tenant_a_id"] = tenant_a.name
            result["tenant_b_id"] = tenant_b.name

            clients_a = _build_tenant_clients(tenant_a, region)
            _wait_for_iam_propagation(clients_a["sts"])

            result["tests"]["network_isolated"] = _probe_network_isolation(ec2, tenant_a, tenant_b)
            result["tests"]["data_isolated"] = _probe_data_isolation(clients_a, tenant_b)
            result["tests"]["compute_isolated"] = _probe_compute_isolation(clients_a, tenant_b)
            result["tests"]["storage_isolated"] = _probe_storage_isolation(clients_a, tenant_a, tenant_b)

            result["success"] = all(t.get("passed") for t in result["tests"].values())
    except (ClientError, WaiterError, BotoCoreError) as exc:
        # Capture the FIRST error before cleanup runs, otherwise a downstream
        # WaiterError from terminating a still-pending instance would mask
        # the real cause (IAM limit, VPC limit, ResourceLimitExceeded, ...).
        error_type, error_msg = classify_aws_error(exc)
        result["error"] = f"[{error_type}] {error_msg}"
        result["success"] = False
    finally:
        cleanup_errors: list[str] = []
        for tenant in (tenant_a, tenant_b):
            if tenant is None:
                continue
            try:
                cleanup_errors.extend(_teardown_tenant(ec2=ec2, iam=iam, kms=kms, s3=s3, tenant=tenant))
            except (ClientError, WaiterError, BotoCoreError) as exc:
                cleanup_errors.append(f"unexpected cleanup error for {tenant.name}: {type(exc).__name__}: {exc}")
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            cleanup_msg = f"Cleanup failed: {'; '.join(cleanup_errors)}"
            existing = result.get("error")
            result["error"] = f"{existing}; {cleanup_msg}" if existing else cleanup_msg
            result["success"] = False

    if skip_payload is not None and not result.get("cleanup_errors"):
        print(json.dumps(skip_payload, indent=2))
        return 0

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
