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

"""Amazon FSx for OpenZFS helpers for home-directory storage tests.

Filesystem-level lifecycle plumbing (describe/wait/delete) is shared with the
Lustre scripts via :mod:`common.fsx`; this module adds only the OpenZFS-specific
pieces: NFS security group, filesystem/volume creation, and volume quotas.
"""

from __future__ import annotations

import time
from typing import Any

from common.errors import TRANSIENT_AWS_CODES, delete_with_retry
from common.fsx import (
    LIFECYCLE_AVAILABLE,
    LIFECYCLE_TERMINAL_BAD,
    delete_filesystem,
    wait_filesystem_deleted,
)

MIN_OPENZFS_CAPACITY_GIB = 64
MIN_OPENZFS_THROUGHPUT_MBPS = 64


def create_nfs_security_group(
    ec2: Any,
    vpc_id: str,
    client_sg_id: str,
    suffix: str,
    created: dict[str, str],
) -> str:
    """Create an NFS security group allowing TCP/2049 from the client group.

    The new group's ID is recorded in ``created`` as soon as it exists so the
    caller's cleanup path can reclaim it if tagging/ingress fails.
    """
    response = ec2.create_security_group(
        GroupName=f"isv-dir-{suffix}",
        Description="NFS access for isvtest home-directory validation",
        VpcId=vpc_id,
    )
    sg_id = response["GroupId"]
    created["sg_id"] = sg_id
    ec2.create_tags(
        Resources=[sg_id],
        Tags=[{"Key": "Name", "Value": f"isv-dir-{suffix}"}, {"Key": "CreatedBy", "Value": "isvtest"}],
    )
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 2049,
                "ToPort": 2049,
                "UserIdGroupPairs": [{"GroupId": client_sg_id}],
            }
        ],
    )
    return sg_id


def create_filesystem(fsx: Any, subnet_id: str, sg_id: str, suffix: str) -> str:
    """Create the minimum-cost Single-AZ FSx for OpenZFS filesystem."""
    response = fsx.create_file_system(
        FileSystemType="OPENZFS",
        StorageType="SSD",
        StorageCapacity=MIN_OPENZFS_CAPACITY_GIB,
        SubnetIds=[subnet_id],
        SecurityGroupIds=[sg_id],
        OpenZFSConfiguration={
            "DeploymentType": "SINGLE_AZ_1",
            "ThroughputCapacity": MIN_OPENZFS_THROUGHPUT_MBPS,
            "AutomaticBackupRetentionDays": 0,
            "CopyTagsToBackups": False,
            "RootVolumeConfiguration": {"DataCompressionType": "NONE"},
        },
        Tags=[
            {"Key": "Name", "Value": f"isv-dir-{suffix}"},
            {"Key": "CreatedBy", "Value": "isvtest"},
        ],
    )
    return str(response["FileSystem"]["FileSystemId"])


def create_test_volume(
    fsx: Any,
    root_volume_id: str,
    client_ip: str,
    suffix: str,
    *,
    quota_gib: int = 2,
) -> str:
    """Create an exported child volume with filesystem, UID, and GID quotas."""
    response = fsx.create_volume(
        VolumeType="OPENZFS",
        Name=f"isvdir{suffix.replace('-', '')}",
        OpenZFSConfiguration={
            "ParentVolumeId": root_volume_id,
            "StorageCapacityQuotaGiB": quota_gib,
            "StorageCapacityReservationGiB": -1,
            "DataCompressionType": "NONE",
            "NfsExports": [
                {
                    "ClientConfigurations": [
                        {
                            "Clients": f"{client_ip}/32",
                            "Options": ["rw", "no_root_squash"],
                        }
                    ]
                }
            ],
            "UserAndGroupQuotas": [
                {"Type": "USER", "Id": 20001, "StorageCapacityQuotaGiB": 1},
                {"Type": "GROUP", "Id": 30001, "StorageCapacityQuotaGiB": 1},
            ],
        },
        Tags=[
            {"Key": "Name", "Value": f"isv-dir-{suffix}"},
            {"Key": "CreatedBy", "Value": "isvtest"},
        ],
    )
    return str(response["Volume"]["VolumeId"])


def describe_volume(fsx: Any, volume_id: str) -> dict[str, Any]:
    """Return a single FSx volume description."""
    response = fsx.describe_volumes(VolumeIds=[volume_id])
    volumes = response.get("Volumes", [])
    if not volumes:
        raise RuntimeError(f"Volume {volume_id} not found")
    return volumes[0]


def wait_volume(
    fsx: Any,
    volume_id: str,
    *,
    expected_quota_gib: int | None = None,
    timeout: float = 900.0,
    delay: float = 10.0,
) -> dict[str, Any]:
    """Wait until an OpenZFS volume is AVAILABLE, optionally with a given quota."""
    deadline = time.monotonic() + timeout
    last_quota: Any = None
    last_lifecycle: Any = None
    while time.monotonic() < deadline:
        volume = describe_volume(fsx, volume_id)
        last_lifecycle = volume.get("Lifecycle")
        last_quota = volume.get("OpenZFSConfiguration", {}).get("StorageCapacityQuotaGiB")
        if last_lifecycle == LIFECYCLE_AVAILABLE and (expected_quota_gib is None or last_quota == expected_quota_gib):
            return volume
        if last_lifecycle in LIFECYCLE_TERMINAL_BAD:
            detail = volume.get("FailureDetails", {}).get("Message", "")
            raise RuntimeError(f"Volume {volume_id} entered {last_lifecycle}: {detail}")
        time.sleep(delay)
    raise TimeoutError(
        f"Timed out waiting for volume {volume_id} "
        f"(expected quota={expected_quota_gib!r}, last quota={last_quota!r}, lifecycle={last_lifecycle!r})"
    )


def set_volume_quota(fsx: Any, volume_id: str, quota_gib: int) -> None:
    """Set the filesystem-wide capacity quota for an OpenZFS volume."""
    fsx.update_volume(
        VolumeId=volume_id,
        OpenZFSConfiguration={"StorageCapacityQuotaGiB": quota_gib},
    )


def cleanup_resources(ec2: Any, fsx: Any, fs_id: str | None, sg_id: str | None) -> list[str]:
    """Best-effort delete the filesystem and NFS security group."""
    errors: list[str] = []
    if fs_id:
        issued = delete_filesystem(
            fsx,
            fs_id,
            wait=False,
            OpenZFSConfiguration={
                "SkipFinalBackup": True,
                "Options": ["DELETE_CHILD_VOLUMES_AND_SNAPSHOTS"],
            },
        )
        if not issued:
            errors.append(f"filesystem {fs_id} cleanup failed")
        elif not wait_filesystem_deleted(fsx, fs_id):
            errors.append(f"filesystem {fs_id} cleanup timed out")
    if sg_id:
        # FSx can disappear from DescribeFileSystems shortly before its ENI
        # releases the security group, so DependencyViolation is transient here.
        deleted = delete_with_retry(
            ec2.delete_security_group,
            GroupId=sg_id,
            resource_desc=f"security group {sg_id}",
            attempts=10,
            backoff_seconds=3,
            transient_codes=TRANSIENT_AWS_CODES | {"DependencyViolation"},
        )
        if not deleted:
            errors.append(f"security group {sg_id} cleanup failed")
    return errors
