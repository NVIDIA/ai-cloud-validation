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

"""DATASVC-XX-02: verify volume snapshots (issue #321).

Snapshots the fixture volume, restores the snapshot to a brand-new volume,
attaches + mounts that restore on the same instance, and byte-compares the
sentinel file against the content the fixture wrote. A successful round-trip
proves the snapshot captured the data and the restore is independently
usable. The restored volume and snapshot are cleaned up in a finally block.

Output JSON:
{
    "success": true,
    "platform": "block_storage",
    "test_name": "snapshot_lifecycle",
    "volume_id": "vol-source",
    "snapshot_id": "snap-xxx",
    "restored_volume_id": "vol-restore",
    "operations": {
        "create_snapshot": {"passed": true},
        "restore_volume":  {"passed": true},
        "verify_data":     {"passed": true, "content_matches": true}
    }
}
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/aws/scripts/ (for common.*)

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from common import ebs
from common.ssh_utils import ssh_run, wait_for_ssh


def _fail(op: dict[str, Any], message: str) -> None:
    """Mark an operation failed with a message."""
    op["passed"] = False
    op["error"] = message


def main() -> int:
    """Snapshot the fixture volume, restore it, verify the data; print JSON."""
    parser = argparse.ArgumentParser(description="Verify volume snapshots (DATASVC-XX-02)")
    parser.add_argument("--instance-id", required=True, help="EC2 instance for the restored volume")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--volume-id", required=True, help="Source (fixture) volume to snapshot")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--expected-content", required=True, help="Sentinel content written by the fixture")
    parser.add_argument("--restore-device", default="/dev/sdg", help="Attach device for the restored volume")
    parser.add_argument("--restore-mount", default="/mnt/isv-restored", help="Mount point for the restored volume")
    args = parser.parse_args()

    operations: dict[str, dict[str, Any]] = {
        "create_snapshot": {"passed": False},
        "restore_volume": {"passed": False},
        "verify_data": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "block_storage",
        "test_name": "snapshot_lifecycle",
        "volume_id": args.volume_id,
        "snapshot_id": None,
        "restored_volume_id": None,
        "operations": operations,
    }

    ec2 = boto3.client("ec2", region_name=args.region)

    snapshot_id: str | None = None
    restored_volume_id: str | None = None
    try:
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        availability_zone = instance["Placement"]["AvailabilityZone"]
        public_ip = instance.get("PublicIpAddress")

        # Flush the guest page cache so the snapshot is consistent with the
        # sentinel write (best-effort; the fixture already synced after write).
        if public_ip:
            ssh_run(public_ip, args.ssh_user, args.key_file, "sudo sync")

        try:
            snapshot_id = ebs.create_snapshot(ec2, args.volume_id, name="isv-validate-snap")
            result["snapshot_id"] = snapshot_id
            ebs.wait_for_snapshot_completed(ec2, snapshot_id)
            operations["create_snapshot"]["passed"] = True
        except (ClientError, NoCredentialsError) as e:
            _fail(operations["create_snapshot"], str(e))
            result["error"] = f"CreateSnapshot failed: {e}"
            return _emit(result)

        try:
            restored_volume_id = ebs.create_volume_from_snapshot(ec2, snapshot_id, availability_zone)
            result["restored_volume_id"] = restored_volume_id
            ebs.wait_for_volume_available(ec2, restored_volume_id)
            ebs.attach_volume(ec2, restored_volume_id, args.instance_id, args.restore_device)
            ebs.wait_for_volume_in_use(ec2, restored_volume_id)
            operations["restore_volume"]["passed"] = True
        except ClientError as e:
            _fail(operations["restore_volume"], str(e))
            result["error"] = f"Restore failed: {e}"
            return _emit(result, ec2, restored_volume_id, snapshot_id)

        if not public_ip or not wait_for_ssh(public_ip, args.ssh_user, args.key_file, max_attempts=30, interval=10):
            _fail(operations["verify_data"], "SSH not ready for restore verification")
            result["error"] = "SSH not ready"
            return _emit(result, ec2, restored_volume_id, snapshot_id)

        if not ebs.wait_for_attachment_device(public_ip, args.ssh_user, args.key_file, restored_volume_id):
            _fail(operations["verify_data"], "Restored volume did not appear in guest")
            return _emit(result, ec2, restored_volume_id, snapshot_id)

        rc, out, err = ebs.mount_and_read_sentinel(
            public_ip, args.ssh_user, args.key_file, restored_volume_id, args.restore_mount
        )
        if rc != 0:
            _fail(operations["verify_data"], f"Could not read sentinel on restore (rc={rc}): {err.strip()[:300]}")
            return _emit(result, ec2, restored_volume_id, snapshot_id)

        content_matches = out.strip() == args.expected_content.strip()
        operations["verify_data"]["content_matches"] = content_matches
        if content_matches:
            operations["verify_data"]["passed"] = True
        else:
            _fail(operations["verify_data"], "Restored sentinel does not match fixture content")

        result["success"] = all(op["passed"] for op in operations.values())
        return _emit(result, ec2, restored_volume_id, snapshot_id)
    except (ClientError, NoCredentialsError) as e:
        result["error"] = str(e)
        return _emit(result, ec2, restored_volume_id, snapshot_id)


def _emit(
    result: dict[str, Any],
    ec2: Any = None,
    restored_volume_id: str | None = None,
    snapshot_id: str | None = None,
) -> int:
    """Best-effort cleanup of the restored volume + snapshot, then print JSON."""
    if ec2 is not None and restored_volume_id:
        err = ebs.detach_and_delete_volume(ec2, restored_volume_id)
        if err:
            result.setdefault("cleanup_errors", []).append(err)
    if ec2 is not None and snapshot_id:
        err = ebs.delete_snapshot_best_effort(ec2, snapshot_id)
        if err:
            result.setdefault("cleanup_errors", []).append(err)
    if result.get("cleanup_errors"):
        result["success"] = False
        cleanup_msg = f"Cleanup failed: {'; '.join(result['cleanup_errors'])}"
        result["error"] = f"{result['error']}; {cleanup_msg}" if result.get("error") else cleanup_msg
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
