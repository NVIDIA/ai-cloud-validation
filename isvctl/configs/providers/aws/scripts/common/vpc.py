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

"""Shared VPC test helpers.

Provides common VPC operations used across network test scripts:
- VPC creation with tagging and optional DNS
- VPC cleanup / deletion
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from common.errors import delete_with_retry


def create_test_vpc(
    ec2: Any,
    cidr: str,
    name: str,
    *,
    enable_dns: bool = False,
) -> dict[str, Any]:
    """Create a tagged test VPC and wait for it to become available.

    Args:
        ec2: Boto3 EC2 client.
        cidr: CIDR block for the VPC (e.g., "10.94.0.0/16").
        name: Name tag for the VPC.
        enable_dns: If True, enable DNS support and hostnames on the VPC.

    Returns:
        Dict with keys: passed, vpc_id, cidr, message/error.
    """
    result: dict[str, Any] = {"passed": False}
    try:
        vpc = ec2.create_vpc(CidrBlock=cidr)
        vpc_id = vpc["Vpc"]["VpcId"]
        result["vpc_id"] = vpc_id  # Set early so finally-block cleanup can find it on partial failure

        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {"Key": "Name", "Value": name},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        waiter = ec2.get_waiter("vpc_available")
        waiter.wait(VpcIds=[vpc_id])

        if enable_dns:
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        result["passed"] = True
        result["cidr"] = cidr
        result["message"] = f"Created VPC {vpc_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def delete_vpc(ec2: Any, vpc_id: str) -> None:
    """Delete a VPC with transient-error retry.

    Routes through ``delete_with_retry`` so a transient throttling or
    endpoint-reset does not leak the VPC on the finally-block path.

    Args:
        ec2: Boto3 EC2 client.
        vpc_id: VPC ID to delete.
    """
    delete_with_retry(
        ec2.delete_vpc,
        VpcId=vpc_id,
        resource_desc=f"VPC {vpc_id}",
    )


def cleanup_vpc_resources(
    ec2: Any,
    vpc_id: str,
    *,
    subnet_ids: list[str] | None = None,
    sg_ids: list[str] | None = None,
    nacl_ids: list[str] | None = None,
) -> None:
    """Clean up VPC and associated resources with transient-error retry.

    Deletes resources in dependency order: SGs -> NACLs -> subnets -> VPC.
    Every delete goes through ``delete_with_retry``, so a transient
    failure on one resource does not orphan the rest of the dependency tree.

    Args:
        ec2: Boto3 EC2 client.
        vpc_id: VPC ID to clean up.
        subnet_ids: Subnet IDs to delete.
        sg_ids: Security group IDs to delete.
        nacl_ids: Network ACL IDs to delete.
    """
    for sg_id in sg_ids or []:
        delete_with_retry(
            ec2.delete_security_group,
            GroupId=sg_id,
            resource_desc=f"security group {sg_id}",
        )

    for nacl_id in nacl_ids or []:
        delete_with_retry(
            ec2.delete_network_acl,
            NetworkAclId=nacl_id,
            resource_desc=f"network ACL {nacl_id}",
        )

    for subnet_id in subnet_ids or []:
        delete_with_retry(
            ec2.delete_subnet,
            SubnetId=subnet_id,
            resource_desc=f"subnet {subnet_id}",
        )

    delete_vpc(ec2, vpc_id)
