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

"""Shared EBS (block volume) helper utilities.

Centralizes the boto3 volume / snapshot lifecycle used by the
block-storage validation scripts (create / attach / snapshot / restore /
resize / detach / delete) plus the Nitro NVMe device-path mapping needed
to find an attached volume from inside the guest.

Device mapping note: on Nitro instances EBS volumes are surfaced as NVMe
devices whose kernel name (``/dev/nvme1n1``) is non-deterministic, but
udev creates a stable by-id symlink that embeds the volume ID with the
dash removed::

    /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0

Scripts resolve that symlink in-guest (``readlink -f``) rather than
guessing ``/dev/sdf`` vs ``/dev/nvme1n1``.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

# Ownership tag so cleanup and audits can tell suite-created volumes apart
# from anything the account already had.
_ISV_CREATED_BY_TAG = {"Key": "CreatedBy", "Value": "isvtest"}

# AWS error codes meaning the volume/snapshot is already gone - treated as
# success in best-effort cleanup paths.
_NOT_FOUND_CODES = frozenset({"InvalidVolume.NotFound", "InvalidSnapshot.NotFound"})


def nvme_serial_for_volume(volume_id: str) -> str:
    """Return the NVMe serial AWS assigns to an EBS volume (the ID minus dashes)."""
    return volume_id.replace("-", "")


def guest_by_id_path(volume_id: str) -> str:
    """Return the stable ``/dev/disk/by-id`` path for an attached EBS volume on Nitro."""
    return f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{nvme_serial_for_volume(volume_id)}"


def _tag_spec(resource_type: str, name: str) -> dict[str, Any]:
    """Build a TagSpecifications entry carrying the Name and ownership tags."""
    return {
        "ResourceType": resource_type,
        "Tags": [{"Key": "Name", "Value": name}, _ISV_CREATED_BY_TAG],
    }


def create_volume(
    ec2: Any,
    availability_zone: str,
    size_gib: int,
    *,
    volume_type: str = "gp3",
    name: str = "isv-validate-block",
) -> str:
    """Create an empty EBS volume in ``availability_zone`` and return its ID."""
    response = ec2.create_volume(
        AvailabilityZone=availability_zone,
        Size=size_gib,
        VolumeType=volume_type,
        TagSpecifications=[_tag_spec("volume", name)],
    )
    return response["VolumeId"]


def create_volume_from_snapshot(
    ec2: Any,
    snapshot_id: str,
    availability_zone: str,
    *,
    volume_type: str = "gp3",
    name: str = "isv-validate-restore",
) -> str:
    """Create a volume restored from ``snapshot_id`` and return its ID."""
    response = ec2.create_volume(
        SnapshotId=snapshot_id,
        AvailabilityZone=availability_zone,
        VolumeType=volume_type,
        TagSpecifications=[_tag_spec("volume", name)],
    )
    return response["VolumeId"]


def attach_volume(ec2: Any, volume_id: str, instance_id: str, device: str) -> None:
    """Attach ``volume_id`` to ``instance_id`` at the requested block device name."""
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device=device)


def detach_volume(ec2: Any, volume_id: str, *, force: bool = False) -> None:
    """Detach ``volume_id`` from whatever instance it is attached to."""
    ec2.detach_volume(VolumeId=volume_id, Force=force)


def delete_volume(ec2: Any, volume_id: str) -> None:
    """Delete ``volume_id`` (must already be detached / available)."""
    ec2.delete_volume(VolumeId=volume_id)


def create_snapshot(
    ec2: Any,
    volume_id: str,
    *,
    description: str = "ISV block-storage validation snapshot",
    name: str = "isv-validate-snap",
) -> str:
    """Create a point-in-time snapshot of ``volume_id`` and return its ID."""
    response = ec2.create_snapshot(
        VolumeId=volume_id,
        Description=description,
        TagSpecifications=[_tag_spec("snapshot", name)],
    )
    return response["SnapshotId"]


def delete_snapshot(ec2: Any, snapshot_id: str) -> None:
    """Delete ``snapshot_id``."""
    ec2.delete_snapshot(SnapshotId=snapshot_id)


def modify_volume_size(ec2: Any, volume_id: str, new_size_gib: int) -> None:
    """Request a grow of ``volume_id`` to ``new_size_gib`` via ModifyVolume."""
    ec2.modify_volume(VolumeId=volume_id, Size=new_size_gib)


def wait_for_volume_available(ec2: Any, volume_id: str, *, delay: int = 5, max_attempts: int = 60) -> None:
    """Block until ``volume_id`` reaches the ``available`` (detached) state."""
    waiter = ec2.get_waiter("volume_available")
    waiter.wait(VolumeIds=[volume_id], WaiterConfig={"Delay": delay, "MaxAttempts": max_attempts})


def wait_for_volume_in_use(ec2: Any, volume_id: str, *, delay: int = 5, max_attempts: int = 60) -> None:
    """Block until ``volume_id`` reaches the ``in-use`` (attached) state."""
    waiter = ec2.get_waiter("volume_in_use")
    waiter.wait(VolumeIds=[volume_id], WaiterConfig={"Delay": delay, "MaxAttempts": max_attempts})


def wait_for_volume_deleted(ec2: Any, volume_id: str, *, delay: int = 5, max_attempts: int = 60) -> None:
    """Block until ``volume_id`` is fully deleted."""
    waiter = ec2.get_waiter("volume_deleted")
    waiter.wait(VolumeIds=[volume_id], WaiterConfig={"Delay": delay, "MaxAttempts": max_attempts})


def wait_for_snapshot_completed(ec2: Any, snapshot_id: str, *, delay: int = 15, max_attempts: int = 80) -> None:
    """Block until ``snapshot_id`` finishes (state ``completed``)."""
    waiter = ec2.get_waiter("snapshot_completed")
    waiter.wait(SnapshotIds=[snapshot_id], WaiterConfig={"Delay": delay, "MaxAttempts": max_attempts})


def wait_for_modification_complete(
    ec2: Any,
    volume_id: str,
    *,
    timeout: int = 900,
    interval: int = 15,
) -> str:
    """Poll ModifyVolume progress until the new size is usable by the guest.

    A volume modification advances ``modifying -> optimizing -> completed``.
    The larger capacity is already visible to the OS once the state reaches
    ``optimizing``, so the in-guest grow can proceed without waiting for the
    (potentially long) background optimization to finish.

    Args:
        ec2: Boto3 EC2 client.
        volume_id: Volume being modified.
        timeout: Total seconds to wait before giving up.
        interval: Seconds between describe calls.

    Returns:
        The terminal modification state observed (``optimizing`` or ``completed``).

    Raises:
        RuntimeError: If the modification fails or does not reach a usable
            state within ``timeout``.
    """
    deadline = time.monotonic() + timeout
    while True:
        response = ec2.describe_volumes_modifications(VolumeIds=[volume_id])
        modifications = response.get("VolumesModifications", [])
        state = modifications[0].get("ModificationState") if modifications else None

        if state in ("optimizing", "completed"):
            return state
        if state == "failed":
            status = modifications[0].get("StatusMessage", "")
            raise RuntimeError(f"Volume modification failed for {volume_id}: {status}")

        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for {volume_id} modification (last state: {state})")
        time.sleep(interval)


def is_volume_attached_to(ec2: Any, volume_id: str, instance_id: str) -> bool:
    """Return True if ``volume_id`` is attached to ``instance_id`` and in-use."""
    response = ec2.describe_volumes(VolumeIds=[volume_id])
    volumes = response.get("Volumes", [])
    if not volumes:
        return False
    volume = volumes[0]
    if volume.get("State") != "in-use":
        return False
    return any(att.get("InstanceId") == instance_id for att in volume.get("Attachments", []))


def detach_and_delete_volume(ec2: Any, volume_id: str) -> str | None:
    """Detach (if needed) and delete ``volume_id`` best-effort.

    Returns an error message on failure, or ``None`` if the volume was
    deleted or was already gone. Safe to call from ``finally`` blocks.
    """
    try:
        try:
            detach_volume(ec2, volume_id, force=True)
            wait_for_volume_available(ec2, volume_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                return None
            # IncorrectState means it is already detached/available - fall through to delete.
            if code != "IncorrectState":
                return str(e)

        delete_volume(ec2, volume_id)
        wait_for_volume_deleted(ec2, volume_id)
        return None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return None
        return str(e)
    except BotoCoreError as e:
        # Waiter timeouts / transport errors must not propagate out of a
        # best-effort cleanup path (callers run this in finally blocks).
        return str(e)


def delete_snapshot_best_effort(ec2: Any, snapshot_id: str) -> str | None:
    """Delete ``snapshot_id`` best-effort. Returns an error message or ``None``."""
    try:
        delete_snapshot(ec2, snapshot_id)
        return None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return None
        return str(e)
    except BotoCoreError as e:
        return str(e)


def wait_for_attachment_device(host: str, user: str, key_file: str, volume_id: str, *, attempts: int = 30) -> bool:
    """Poll over SSH until the volume's by-id symlink exists in the guest.

    Returns True once ``guest_by_id_path(volume_id)`` is present, else False.
    """
    # Local import to avoid a hard dependency for callers that only need the
    # boto3 helpers (and to keep the common package import-light).
    from common.ssh_utils import ssh_run

    by_id = guest_by_id_path(volume_id)
    for attempt in range(1, attempts + 1):
        rc, _, _ = ssh_run(host, user, key_file, f"test -e {by_id}")
        if rc == 0:
            return True
        print(f"  Waiting for {by_id} in guest... (attempt {attempt}/{attempts})", file=sys.stderr)
        time.sleep(5)
    return False


# In-guest: resolve the volume's partition via its stable by-id symlink, mount
# it (idempotently), and print the sentinel file contents to stdout.
SENTINEL_FILENAME = "isv-sentinel.txt"
_READ_SENTINEL_SCRIPT = r"""
set -euo pipefail
BYID="__BYID__"
MOUNT="__MOUNT__"
for _ in $(seq 1 30); do [ -e "__BYID__-part1" ] && break; sleep 2; done
PART=$(readlink -f "__BYID__-part1")
sudo mkdir -p "$MOUNT"
mountpoint -q "$MOUNT" || sudo mount "$PART" "$MOUNT"
cat "$MOUNT/__FILENAME__"
"""


def mount_and_read_sentinel(
    host: str,
    user: str,
    key_file: str,
    volume_id: str,
    mount_point: str,
    *,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Mount ``volume_id`` in the guest and read the sentinel file.

    Returns ``(exit_code, stdout, stderr)`` from the remote command. ``stdout``
    holds the raw sentinel contents on success (callers byte-compare it).
    """
    # Local import keeps the common package import-light for boto3-only callers.
    from common.ssh_utils import ssh_run

    script = (
        _READ_SENTINEL_SCRIPT.replace("__BYID__", guest_by_id_path(volume_id))
        .replace("__MOUNT__", mount_point)
        .replace("__FILENAME__", SENTINEL_FILENAME)
    )
    return ssh_run(host, user, key_file, script, timeout=timeout)
