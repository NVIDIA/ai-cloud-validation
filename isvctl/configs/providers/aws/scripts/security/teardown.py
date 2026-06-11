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

"""Security test teardown (AWS reference).

Each individual test script handles its own cleanup. This teardown step
is a safety net that scans for leftover resources that owned-prefix
test scripts may have leaked on a hard crash:

* IAM users
    * ``isv-sa-test-*``     - sa_credential_test.py
    * ``isv-sec02-test-*``  - short_lived_credentials_test.py
    * ``isv-sec04-test-*``  - least_privilege_test.py
    * ``isv-sec11-test-*``  - tenant_isolation_test.py
* SEC04/SEC11/SEC13 fixtures (owned prefix + ``CreatedBy=isvtest`` tag):
    * EC2 instances, EBS volumes, security groups, subnets, VPCs
    * SEC13-only: NLBs, target groups, IAM server certs, IGWs, route tables
    * KMS aliases (SEC11 only, and the keys they target)
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
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError, WaiterError
from common.errors import TRANSIENT_AWS_CODES, delete_with_retry, handle_aws_errors

OWNED_USER_PREFIXES: tuple[str, ...] = ("isv-sa-test-", "isv-sec02-test-", "isv-sec04-test-", "isv-sec11-test-")

SEC04_PREFIX = "isv-sec04-test-"
SEC11_PREFIX = "isv-sec11-test-"
SEC13_PREFIX = "isv-sec13-test-"
OWNED_RESOURCE_PREFIXES: tuple[str, ...] = (SEC04_PREFIX, SEC11_PREFIX, SEC13_PREFIX)
SEC11_KMS_ALIAS_PREFIX = f"alias/{SEC11_PREFIX}"
ELBV2_READ_PERMISSION_ERRORS = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"})
ISVTEST_TAG_FILTER = [
    {"Name": "tag:CreatedBy", "Values": ["isvtest"]},
]
CERT_DELETE_TRANSIENT_CODES = TRANSIENT_AWS_CODES | frozenset({"DeleteConflict"})
NLB_RESOURCE_RELEASE_TRANSIENT_CODES = TRANSIENT_AWS_CODES | frozenset({"DependencyViolation"})
NLB_RESOURCE_RELEASE_RETRY_ATTEMPTS = 10
NLB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS = 3.0


def _is_elbv2_read_permission_error(error: ClientError) -> bool:
    """Return True when ELBv2 read access is unavailable for the SEC13 sweep."""
    code = error.response.get("Error", {}).get("Code", "")
    return code in ELBV2_READ_PERMISSION_ERRORS


def _detach_internet_gateway(ec2: Any, *, internet_gateway_id: str, vpc_id: str) -> None:
    """Detach an IGW, treating already-detached/not-found as successful cleanup."""
    try:
        ec2.detach_internet_gateway(InternetGatewayId=internet_gateway_id, VpcId=vpc_id)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"Gateway.NotAttached", "InvalidInternetGatewayID.NotFound"}:
            return
        raise


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


def _resource_has_owned_name(tags: list[dict[str, str]] | None) -> bool:
    """Return True if the EC2 ``Tags`` contain a Name with an owned security-test prefix."""
    if not tags:
        return False
    return any(t.get("Key") == "Name" and (t.get("Value") or "").startswith(OWNED_RESOURCE_PREFIXES) for t in tags)


def _cleanup_owned_instances(ec2: Any) -> list[str]:
    """Terminate leftover owned EC2 instances and wait for the terminated state."""
    errors: list[str] = []
    instance_ids: list[str] = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=ISVTEST_TAG_FILTER):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                if instance.get("State", {}).get("Name") == "terminated":
                    continue
                if not _resource_has_owned_name(instance.get("Tags")):
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


def _cleanup_owned_volumes(ec2: Any) -> list[str]:
    """Delete leftover owned EBS volumes (must run AFTER instance termination)."""
    errors: list[str] = []
    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=ISVTEST_TAG_FILTER):
        for volume in page.get("Volumes", []):
            if not _resource_has_owned_name(volume.get("Tags")):
                continue
            volume_id = volume["VolumeId"]
            if not delete_with_retry(
                ec2.delete_volume,
                VolumeId=volume_id,
                resource_desc=f"volume {volume_id}",
            ):
                errors.append(f"delete volume {volume_id} failed")
    return errors


def _cleanup_owned_vpcs(ec2: Any) -> list[str]:
    """Delete leftover owned VPCs and their dependencies (SGs, subnets, IGWs, route tables)."""
    errors: list[str] = []
    vpcs = ec2.describe_vpcs(Filters=ISVTEST_TAG_FILTER).get("Vpcs", [])
    for vpc in vpcs:
        if not _resource_has_owned_name(vpc.get("Tags")):
            continue
        vpc_id = vpc["VpcId"]

        # Custom route tables (skip the main route table; it gets deleted
        # with the VPC). Subnet associations are dropped implicitly when
        # the subnet is deleted, so no explicit disassociate is required.
        route_tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get(
            "RouteTables", []
        )
        for rt in route_tables:
            if any(assoc.get("Main") for assoc in rt.get("Associations", [])):
                continue
            for assoc in rt.get("Associations", []):
                assoc_id = assoc.get("RouteTableAssociationId")
                if not assoc_id:
                    continue
                try:
                    ec2.disassociate_route_table(AssociationId=assoc_id)
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") != "InvalidAssociationID.NotFound":
                        errors.append(f"disassociate route table {assoc_id}: {e}")
            if not delete_with_retry(
                ec2.delete_route_table,
                RouteTableId=rt["RouteTableId"],
                resource_desc=f"route table {rt['RouteTableId']}",
            ):
                errors.append(f"delete route table {rt['RouteTableId']} failed")

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
                attempts=NLB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
                backoff_seconds=NLB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
                transient_codes=NLB_RESOURCE_RELEASE_TRANSIENT_CODES,
            ):
                errors.append(f"delete subnet {subnet['SubnetId']} failed")

        # IGWs must be detached + deleted before the VPC. Do this after
        # subnet cleanup so NLB-managed public addresses have the best
        # chance to disappear before DetachInternetGateway is attempted.
        igws = ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}],
        ).get("InternetGateways", [])
        for igw in igws:
            igw_id = igw["InternetGatewayId"]
            if not delete_with_retry(
                _detach_internet_gateway,
                ec2,
                internet_gateway_id=igw_id,
                vpc_id=vpc_id,
                resource_desc=f"detach IGW {igw_id}",
                attempts=NLB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
                backoff_seconds=NLB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
                transient_codes=NLB_RESOURCE_RELEASE_TRANSIENT_CODES,
            ):
                errors.append(f"detach IGW {igw_id} failed")
            if not delete_with_retry(
                ec2.delete_internet_gateway,
                InternetGatewayId=igw_id,
                resource_desc=f"IGW {igw_id}",
                attempts=NLB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
                backoff_seconds=NLB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
                transient_codes=NLB_RESOURCE_RELEASE_TRANSIENT_CODES,
            ):
                errors.append(f"delete IGW {igw_id} failed")

        if not delete_with_retry(
            ec2.delete_vpc,
            VpcId=vpc_id,
            resource_desc=f"VPC {vpc_id}",
            attempts=NLB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
            backoff_seconds=NLB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
            transient_codes=NLB_RESOURCE_RELEASE_TRANSIENT_CODES,
        ):
            errors.append(f"delete VPC {vpc_id} failed")
    return errors


def _cleanup_owned_load_balancers(elbv2: Any) -> list[str]:
    """Delete leftover owned NLBs (SEC13). Listeners are removed implicitly with the LB."""
    errors: list[str] = []
    try:
        paginator = elbv2.get_paginator("describe_load_balancers")
        lbs: list[dict[str, Any]] = []
        for page in paginator.paginate():
            lbs.extend(page.get("LoadBalancers", []))
    except ClientError as e:
        if _is_elbv2_read_permission_error(e):
            return []
        return [f"describe load balancers: {e}"]

    if not lbs:
        return errors

    arns = [lb["LoadBalancerArn"] for lb in lbs]
    arn_to_lb = {lb["LoadBalancerArn"]: lb for lb in lbs}

    owned_arns: list[str] = []
    # describe_tags can take up to 20 ARNs per call.
    for i in range(0, len(arns), 20):
        batch = arns[i : i + 20]
        try:
            tag_descs = elbv2.describe_tags(ResourceArns=batch).get("TagDescriptions", [])
        except ClientError as e:
            errors.append(f"describe LB tags {batch}: {e}")
            continue
        for td in tag_descs:
            tags = {t["Key"]: t["Value"] for t in td.get("Tags", [])}
            if tags.get("CreatedBy") != "isvtest":
                continue
            if not (tags.get("Name", "") or "").startswith(OWNED_RESOURCE_PREFIXES):
                continue
            owned_arns.append(td["ResourceArn"])

    for arn in owned_arns:
        try:
            elbv2.delete_load_balancer(LoadBalancerArn=arn)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "LoadBalancerNotFound":
                errors.append(f"delete load balancer {arn}: {e}")
                continue
        try:
            elbv2.get_waiter("load_balancers_deleted").wait(
                LoadBalancerArns=[arn],
                WaiterConfig={"Delay": 10, "MaxAttempts": 30},
            )
        except (ClientError, WaiterError) as e:
            errors.append(f"wait load balancer deleted {arn}: {e}")
            continue
        # Best-effort log line for owned-LB cleanup parity with other sweeps.
        _ = arn_to_lb.get(arn)
    return errors


def _cleanup_owned_target_groups(elbv2: Any) -> list[str]:
    """Delete leftover owned target groups (SEC13). LBs must be gone first."""
    errors: list[str] = []
    try:
        paginator = elbv2.get_paginator("describe_target_groups")
        tgs: list[dict[str, Any]] = []
        for page in paginator.paginate():
            tgs.extend(page.get("TargetGroups", []))
    except ClientError as e:
        if _is_elbv2_read_permission_error(e):
            return []
        return [f"describe target groups: {e}"]

    if not tgs:
        return errors

    arns = [tg["TargetGroupArn"] for tg in tgs]
    owned_arns: list[str] = []
    for i in range(0, len(arns), 20):
        batch = arns[i : i + 20]
        try:
            tag_descs = elbv2.describe_tags(ResourceArns=batch).get("TagDescriptions", [])
        except ClientError as e:
            errors.append(f"describe TG tags {batch}: {e}")
            continue
        for td in tag_descs:
            tags = {t["Key"]: t["Value"] for t in td.get("Tags", [])}
            if tags.get("CreatedBy") != "isvtest":
                continue
            if not (tags.get("Name", "") or "").startswith(OWNED_RESOURCE_PREFIXES):
                continue
            owned_arns.append(td["ResourceArn"])

    for arn in owned_arns:
        if not delete_with_retry(
            elbv2.delete_target_group,
            TargetGroupArn=arn,
            resource_desc=f"target group {arn}",
        ):
            errors.append(f"delete target group {arn} failed")
    return errors


def _cleanup_owned_iam_server_certs(iam: Any) -> list[str]:
    """Delete leftover owned IAM server certificates (SEC13 fixture leak)."""
    errors: list[str] = []
    try:
        paginator = iam.get_paginator("list_server_certificates")
        for page in paginator.paginate():
            for meta in page.get("ServerCertificateMetadataList", []):
                name = meta.get("ServerCertificateName", "")
                if not name.startswith(SEC13_PREFIX):
                    continue
                if not delete_with_retry(
                    iam.delete_server_certificate,
                    ServerCertificateName=name,
                    resource_desc=f"IAM server cert {name}",
                    attempts=NLB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
                    backoff_seconds=NLB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
                    transient_codes=CERT_DELETE_TRANSIENT_CODES,
                ):
                    errors.append(f"delete IAM server cert {name} failed")
    except ClientError as e:
        errors.append(f"list server certificates: {e}")
    return errors


def _cleanup_owned_kms(kms: Any) -> list[str]:
    """Schedule owned KMS keys for deletion and remove their aliases.

    Scope is currently SEC11 only (``alias/isv-sec11-test-*``); SEC04 does not
    create KMS resources.
    """
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


def _cleanup_owned_buckets(s3: Any) -> list[str]:
    """Empty and delete leftover owned S3 buckets."""
    errors: list[str] = []
    try:
        buckets = s3.list_buckets().get("Buckets", [])
    except ClientError as e:
        return [f"list_buckets: {e}"]

    for bucket in buckets:
        name = bucket.get("Name", "")
        if not name.startswith(OWNED_RESOURCE_PREFIXES):
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
    elbv2 = boto3.client("elbv2", region_name=args.region)
    kms = boto3.client("kms", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)

    resource_errors: list[str] = []

    # Order matters:
    #  - LBs first so NLB ENIs release before VPC sweep tries to delete
    #    the subnets / security groups they were attached to.
    #  - Target groups can only be deleted after their LB is gone.
    #  - Instances before volumes (so volumes can be deleted).
    #  - VPCs after compute is gone (subnets/SGs unreferenced).
    #  - IAM server certs after their LB is gone (delete-conflict otherwise).
    try:
        resource_errors.extend(_cleanup_owned_load_balancers(elbv2))
        resource_errors.extend(_cleanup_owned_target_groups(elbv2))
        resource_errors.extend(_cleanup_owned_instances(ec2))
        resource_errors.extend(_cleanup_owned_volumes(ec2))
        resource_errors.extend(_cleanup_owned_vpcs(ec2))
        resource_errors.extend(_cleanup_owned_iam_server_certs(iam))
        resource_errors.extend(_cleanup_owned_kms(kms))
        resource_errors.extend(_cleanup_owned_buckets(s3))
    except ClientError as e:
        resource_errors.append(str(e))

    cleaned = 0
    skipped_unowned = 0
    failed_resources: list[dict[str, Any]] = []
    try:
        cleaned, skipped_unowned, failed_resources = _sweep_iam_users(iam)
    except ClientError as e:
        result["error"] = str(e)

    result["resources_cleaned"] = cleaned
    result["resources_skipped_unowned"] = skipped_unowned
    if resource_errors:
        result["security_resource_cleanup_errors"] = resource_errors
    if failed_resources:
        result["resources_failed"] = failed_resources

    if failed_resources or resource_errors:
        result["success"] = False
        existing_error = result.get("error", "")
        msgs: list[str] = []
        if failed_resources:
            msgs.append(
                f"Failed to clean up {len(failed_resources)} owned IAM user(s): "
                + "; ".join(f"{item['username']}: {', '.join(item['errors'])}" for item in failed_resources)
            )
        if resource_errors:
            msgs.append("security resource sweep errors: " + "; ".join(resource_errors))
        combined = "; ".join(msgs)
        result["error"] = f"{existing_error}; {combined}" if existing_error else combined
    elif "error" not in result:
        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
