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

"""Shared Amazon FSx for Lustre helpers for the AWS storage (HSS) scripts.

FSx for Lustre is AWS's managed parallel high-speed filesystem, so it is the
natural backend for the HSS ("High-Speed Storage") validations. These helpers
create a minimal network (VPC + subnet + Lustre-port security group), provision
and poll Lustre filesystems, and tear everything down. Every script that uses
them keeps the provider-neutral JSON contract the validations assert on.

Lustre notes:
  - PERSISTENT_2 is used (supports provisioned throughput and root-squash).
  - Minimum StorageCapacity for PERSISTENT_2/125 is 1200 GiB (~1.2 TiB), which
    is comfortably <= the 50 TiB minimum-filesystem-size ceiling HSS09 checks.
  - RootSquash is formatted "UID:GID"; "0:0" disables squashing (root stays root).
  - Storage capacity increases may briefly set Lifecycle=UPDATING; wait until
    StorageCapacity reflects the target and Lifecycle returns to AVAILABLE.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from botocore.exceptions import ClientError

from common.errors import delete_with_retry
from common.vpc import cleanup_vpc_resources, create_test_vpc

# Lustre client<->server ports (LNet). Opened intra-VPC so a mount client in the
# same network can reach the filesystem; harmless for provisioning-only checks.
LUSTRE_PORTS: tuple[tuple[int, int], ...] = ((988, 988), (1018, 1023))

# FSx for Lustre PERSISTENT_2/125 minimum StorageCapacity (GiB).
MIN_LUSTRE_CAPACITY_GIB = 1200

# FSx filesystem lifecycle states.
LIFECYCLE_AVAILABLE = "AVAILABLE"
LIFECYCLE_TERMINAL_BAD = frozenset({"FAILED", "MISCONFIGURED", "MISCONFIGURED_UNAVAILABLE"})


def new_suffix() -> str:
    """Return a short unique suffix for resource names."""
    return str(uuid.uuid4())[:8]


def create_fsx_network(ec2: Any, cidr: str, suffix: str, created: dict[str, Any]) -> dict[str, Any]:
    """Create a VPC, one subnet, and a Lustre-port security group.

    Resource IDs are recorded in ``created`` as soon as they exist so the
    caller's cleanup path can reclaim partially-created resources on failure.

    Returns:
        Dict with vpc_id, subnet_id, sg_id.
    """
    vpc = create_test_vpc(ec2, cidr, f"isv-fsx-{suffix}", enable_dns=True)
    vpc_id = vpc.get("vpc_id")
    created["vpc_id"] = vpc_id
    if not vpc["passed"]:
        raise RuntimeError(vpc.get("error", "VPC creation failed"))

    azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
    zone_names = [z["ZoneName"] for z in azs["AvailabilityZones"]]
    if not zone_names:
        raise RuntimeError("No availability zones found for FSx subnet")

    # A single /24 subnet is enough for a managed FSx filesystem.
    subnet_cidr = cidr.rsplit(".", 2)[0] + ".0.0/24" if cidr.endswith("/16") else cidr
    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=zone_names[0])
    subnet_id = subnet["Subnet"]["SubnetId"]
    created.setdefault("subnet_ids", []).append(subnet_id)
    ec2.create_tags(
        Resources=[subnet_id],
        Tags=[{"Key": "Name", "Value": f"isv-fsx-{suffix}"}, {"Key": "CreatedBy", "Value": "isvtest"}],
    )

    sg = ec2.create_security_group(
        GroupName=f"isv-fsx-{suffix}",
        Description="FSx for Lustre validation (intra-VPC Lustre ports)",
        VpcId=vpc_id,
    )
    sg_id = sg["GroupId"]
    created.setdefault("sg_ids", []).append(sg_id)
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": lo, "ToPort": hi, "IpRanges": [{"CidrIp": cidr}]}
            for lo, hi in LUSTRE_PORTS
        ],
    )
    return {"vpc_id": vpc_id, "subnet_id": subnet_id, "sg_id": sg_id}


def cleanup_fsx_network(ec2: Any, created: dict[str, Any]) -> None:
    """Best-effort teardown of the VPC/subnet/SG created by ``create_fsx_network``."""
    vpc_id = created.get("vpc_id")
    if not vpc_id:
        return
    cleanup_vpc_resources(
        ec2,
        vpc_id,
        subnet_ids=created.get("subnet_ids"),
        sg_ids=created.get("sg_ids"),
    )


def create_lustre_filesystem(
    fsx: Any,
    subnet_id: str,
    sg_ids: list[str],
    storage_capacity: int,
    *,
    per_unit_throughput: int = 125,
    deployment_type: str = "PERSISTENT_2",
    root_squash: str | None = None,
    name: str = "isv-fsx-lustre",
    suffix: str | None = None,
) -> str:
    """Create an FSx for Lustre filesystem and return its FileSystemId.

    Does not wait for AVAILABLE - call :func:`wait_filesystem_available`.
    """
    lustre_config: dict[str, Any] = {
        "DeploymentType": deployment_type,
        "PerUnitStorageThroughput": per_unit_throughput,
    }
    if root_squash is not None:
        lustre_config["RootSquashConfiguration"] = {"RootSquash": root_squash}

    resp = fsx.create_file_system(
        FileSystemType="LUSTRE",
        StorageCapacity=storage_capacity,
        SubnetIds=[subnet_id],
        SecurityGroupIds=sg_ids,
        LustreConfiguration=lustre_config,
        Tags=[
            {"Key": "Name", "Value": f"{name}-{suffix}" if suffix else name},
            {"Key": "CreatedBy", "Value": "isvtest"},
        ],
    )
    return resp["FileSystem"]["FileSystemId"]


def describe_filesystem(fsx: Any, fs_id: str) -> dict[str, Any] | None:
    """Return the FSx filesystem description, or None if it no longer exists."""
    try:
        resp = fsx.describe_file_systems(FileSystemIds=[fs_id])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "FileSystemNotFound":
            return None
        raise
    systems = resp.get("FileSystems", [])
    return systems[0] if systems else None


def _raise_if_terminal(fs: dict[str, Any], fs_id: str, context: str) -> None:
    """Raise RuntimeError if the filesystem is in a terminal failure lifecycle."""
    lifecycle = fs.get("Lifecycle")
    if lifecycle in LIFECYCLE_TERMINAL_BAD:
        details = fs.get("FailureDetails", {}).get("Message", "")
        raise RuntimeError(f"Filesystem {fs_id} entered {lifecycle} {context}: {details}")


def wait_filesystem_available(fsx: Any, fs_id: str, *, timeout: float = 1800.0, delay: float = 15.0) -> dict[str, Any]:
    """Poll until the filesystem is AVAILABLE; raise on failure or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fs = describe_filesystem(fsx, fs_id)
        if fs is None:
            raise RuntimeError(f"Filesystem {fs_id} disappeared while waiting for AVAILABLE")
        if fs.get("Lifecycle") == LIFECYCLE_AVAILABLE:
            return fs
        _raise_if_terminal(fs, fs_id, "while waiting for AVAILABLE")
        time.sleep(delay)
    raise TimeoutError(f"Timed out waiting for filesystem {fs_id} to become AVAILABLE")


def wait_root_squash(fsx: Any, fs_id: str, expected: str, *, timeout: float = 600.0, delay: float = 10.0) -> bool:
    """Poll until the filesystem's RootSquash setting equals ``expected``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fs = describe_filesystem(fsx, fs_id)
        if fs is not None:
            current = fs.get("LustreConfiguration", {}).get("RootSquashConfiguration", {}).get("RootSquash")
            if current == expected:
                return True
        time.sleep(delay)
    return False


def wait_storage_capacity(fsx: Any, fs_id: str, at_least: int, *, timeout: float = 1800.0, delay: float = 15.0) -> int:
    """Poll until StorageCapacity >= ``at_least`` and Lifecycle is AVAILABLE.

    FSx for Lustre may briefly enter ``UPDATING`` while adding capacity; the new
    ``StorageCapacity`` is visible once scaling finishes and the filesystem
    returns to ``AVAILABLE`` (background ``STORAGE_OPTIMIZATION`` may still run).

    Raises:
        RuntimeError: Filesystem entered a terminal failure lifecycle.
        TimeoutError: Capacity/AVAILABLE not reached before ``timeout``.
    """
    deadline = time.monotonic() + timeout
    capacity = 0
    lifecycle = "UNKNOWN"
    while time.monotonic() < deadline:
        fs = describe_filesystem(fsx, fs_id)
        if fs is None:
            raise RuntimeError(f"Filesystem {fs_id} disappeared during capacity increase")
        _raise_if_terminal(fs, fs_id, "during capacity increase")
        capacity = fs.get("StorageCapacity", 0)
        lifecycle = fs.get("Lifecycle", "UNKNOWN")
        if capacity >= at_least and lifecycle == LIFECYCLE_AVAILABLE:
            return capacity
        time.sleep(delay)
    raise TimeoutError(
        f"Timed out waiting for filesystem {fs_id} capacity >= {at_least} GiB "
        f"(last StorageCapacity={capacity}, Lifecycle={lifecycle})"
    )


def wait_filesystem_deleted(fsx: Any, fs_id: str, *, timeout: float = 900.0, delay: float = 15.0) -> bool:
    """Poll until the filesystem no longer exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if describe_filesystem(fsx, fs_id) is None:
            return True
        time.sleep(delay)
    return False


def delete_filesystem(
    fsx: Any,
    fs_id: str,
    *,
    wait: bool = True,
    timeout: float = 900.0,
    delay: float = 15.0,
    **delete_kwargs: Any,
) -> bool:
    """Best-effort delete of an FSx filesystem, optionally waiting for removal.

    Extra keyword args (e.g. ``OpenZFSConfiguration``) are forwarded to
    ``delete_file_system``.
    """
    ok = delete_with_retry(
        fsx.delete_file_system, FileSystemId=fs_id, resource_desc=f"FSx filesystem {fs_id}", **delete_kwargs
    )
    if not ok or not wait:
        return ok
    return wait_filesystem_deleted(fsx, fs_id, timeout=timeout, delay=delay)


def cleanup_fsx_resources(ec2: Any, fsx: Any, fs_ids: list[str], created: dict[str, Any]) -> list[str]:
    """Best-effort teardown of FSx filesystems and their network; return errors.

    All deletes are issued before any wait so multiple filesystems delete
    concurrently server-side. The network is torn down last because the
    security group cannot be removed while a filesystem still uses it.
    """
    errors: list[str] = []
    issued: list[str] = []
    for fs_id in fs_ids:
        if delete_filesystem(fsx, fs_id, wait=False):
            issued.append(fs_id)
        else:
            errors.append(f"filesystem {fs_id} cleanup failed")
    for fs_id in issued:
        if not wait_filesystem_deleted(fsx, fs_id):
            errors.append(f"filesystem {fs_id} cleanup failed")
    try:
        cleanup_fsx_network(ec2, created)
    except Exception as e:
        errors.append(f"network cleanup failed: {e}")
    return errors
