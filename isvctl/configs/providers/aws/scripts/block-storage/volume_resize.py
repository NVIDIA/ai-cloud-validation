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

"""DATASVC-XX-03: verify volume resizing (issue #322).

Grows the fixture volume with ModifyVolume, then grows the in-guest
partition (growpart) and filesystem (resize2fs) and confirms the larger
capacity is visible to the guest. Online resize keeps the volume mounted
throughout, so no data is lost.

Output JSON:
{
    "success": true,
    "platform": "block_storage",
    "test_name": "volume_resize",
    "volume_id": "vol-xxx",
    "old_size_gib": 10,
    "new_size_gib": 15,
    "fs_bytes_before": 10434441216,
    "fs_bytes_after": 15728623616,
    "operations": {
        "modify_volume":     {"passed": true},
        "grow_partition":    {"passed": true},
        "resize_filesystem": {"passed": true},
        "verify_size":       {"passed": true}
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

_READ_FS_BYTES = r"""
set -euo pipefail
MOUNT="__MOUNT__"
df -B1 --output=size "$MOUNT" | tail -1 | tr -d ' '
"""

_GROWPART = r"""
set -euo pipefail
DEV=$(readlink -f "__BYID__")
sudo growpart "$DEV" 1
sudo partprobe "$DEV" || true
sudo udevadm settle || true
"""

_RESIZE2FS = r"""
set -euo pipefail
PART=$(readlink -f "__BYID__-part1")
sudo resize2fs "$PART"
"""


def _fail(op: dict[str, Any], message: str) -> None:
    """Mark an operation failed with a message."""
    op["passed"] = False
    op["error"] = message


def main() -> int:
    """Grow the fixture volume + in-guest filesystem and verify; print JSON."""
    parser = argparse.ArgumentParser(description="Verify volume resizing (DATASVC-XX-03)")
    parser.add_argument("--instance-id", required=True, help="EC2 instance the volume is attached to")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--volume-id", required=True, help="Volume to resize (fixture volume)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="/mnt/isv-block", help="In-guest mount point")
    parser.add_argument("--grow-gib", type=int, default=5, help="GiB to add to the volume")
    args = parser.parse_args()

    operations: dict[str, dict[str, Any]] = {
        "modify_volume": {"passed": False},
        "grow_partition": {"passed": False},
        "resize_filesystem": {"passed": False},
        "verify_size": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "block_storage",
        "test_name": "volume_resize",
        "volume_id": args.volume_id,
        "operations": operations,
    }

    ec2 = boto3.client("ec2", region_name=args.region)
    by_id = ebs.guest_by_id_path(args.volume_id)

    try:
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        public_ip = instances["Reservations"][0]["Instances"][0].get("PublicIpAddress")

        volumes = ec2.describe_volumes(VolumeIds=[args.volume_id])
        old_size = volumes["Volumes"][0]["Size"]
        new_size = old_size + args.grow_gib
        result["old_size_gib"] = old_size
        result["new_size_gib"] = new_size

        try:
            ebs.modify_volume_size(ec2, args.volume_id, new_size)
            ebs.wait_for_modification_complete(ec2, args.volume_id)
            operations["modify_volume"]["passed"] = True
        except (ClientError, NoCredentialsError, RuntimeError) as e:
            _fail(operations["modify_volume"], str(e))
            result["error"] = f"ModifyVolume failed: {e}"
            print(json.dumps(result, indent=2))
            return 1
    except (ClientError, NoCredentialsError) as e:
        result["error"] = str(e)
        print(json.dumps(result, indent=2))
        return 1

    if not public_ip or not wait_for_ssh(public_ip, args.ssh_user, args.key_file, max_attempts=30, interval=10):
        result["error"] = "SSH not ready for in-guest resize"
        print(json.dumps(result, indent=2))
        return 1

    rc, before_out, _ = ssh_run(public_ip, args.ssh_user, args.key_file, _READ_FS_BYTES.replace("__MOUNT__", args.mount_point))
    fs_before = int(before_out.strip()) if rc == 0 and before_out.strip().isdigit() else None
    result["fs_bytes_before"] = fs_before

    rc, _, err = ssh_run(public_ip, args.ssh_user, args.key_file, _GROWPART.replace("__BYID__", by_id))
    if rc == 0:
        operations["grow_partition"]["passed"] = True
    else:
        _fail(operations["grow_partition"], f"growpart failed (rc={rc}): {err.strip()[:300]}")

    if operations["grow_partition"]["passed"]:
        rc, _, err = ssh_run(public_ip, args.ssh_user, args.key_file, _RESIZE2FS.replace("__BYID__", by_id))
        if rc == 0:
            operations["resize_filesystem"]["passed"] = True
        else:
            _fail(operations["resize_filesystem"], f"resize2fs failed (rc={rc}): {err.strip()[:300]}")

    rc, after_out, _ = ssh_run(public_ip, args.ssh_user, args.key_file, _READ_FS_BYTES.replace("__MOUNT__", args.mount_point))
    fs_after = int(after_out.strip()) if rc == 0 and after_out.strip().isdigit() else None
    result["fs_bytes_after"] = fs_after

    ebs_grew = ec2.describe_volumes(VolumeIds=[args.volume_id])["Volumes"][0]["Size"] == result["new_size_gib"]
    fs_grew = fs_before is not None and fs_after is not None and fs_after > fs_before
    if ebs_grew and fs_grew:
        operations["verify_size"]["passed"] = True
    else:
        _fail(
            operations["verify_size"],
            f"Size did not grow as expected (ebs_grew={ebs_grew}, fs_before={fs_before}, fs_after={fs_after})",
        )

    result["success"] = all(op["passed"] for op in operations.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
