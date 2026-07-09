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

"""Block-storage fixture: create, attach, format, mount, and seed a volume.

Shared setup step for the storage suite (DATASVC-XX-02/03/04). It
creates an EBS volume in the instance's AZ, attaches it, partitions +
formats it (ext4), mounts it, and writes a sentinel file. The volume ID,
mount point, and sentinel content are passed to the snapshot / resize /
persistence test steps, which all reuse this single fixture.

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "create_volume",
    "volume_id": "vol-xxx",
    "device": "/dev/sdf",
    "mount_point": "/mnt/isv-block",
    "size_gib": 10,
    "sentinel_path": "/mnt/isv-block/isv-sentinel.txt",
    "sentinel_content": "isv-ncp-validate-storage-...",
    "operations": {
        "create":         {"passed": true},
        "attach":         {"passed": true},
        "format":         {"passed": true},
        "mount":          {"passed": true},
        "write_sentinel": {"passed": true}
    }
}
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/aws/scripts/ (for common.*)

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from common import ebs
from common.ssh_utils import ssh_run, wait_for_ssh

# Remote setup: resolve the attached device via its stable by-id symlink,
# lay down a single GPT partition, format it ext4, mount it, and write the
# sentinel file. Placeholders are substituted in Python (avoids f-string vs
# shell brace conflicts).
_SETUP_SCRIPT = r"""
set -euo pipefail
BYID="__BYID__"
MOUNT="__MOUNT__"
SENTINEL="$MOUNT/isv-sentinel.txt"
CONTENT="__CONTENT__"
for _ in $(seq 1 30); do [ -e "$BYID" ] && break; sleep 2; done
DEV=$(readlink -f "$BYID")
sudo parted -s "$DEV" mklabel gpt
sudo parted -s "$DEV" mkpart primary ext4 0% 100%
sudo partprobe "$DEV" || true
sudo udevadm settle || true
for _ in $(seq 1 30); do [ -e "__BYID__-part1" ] && break; sleep 2; done
PART=$(readlink -f "__BYID__-part1")
sudo mkfs.ext4 -F "$PART"
sudo mkdir -p "$MOUNT"
sudo mount "$PART" "$MOUNT"
echo -n "$CONTENT" | sudo tee "$SENTINEL" >/dev/null
sudo sync
"""


def _render(script: str, **subs: str) -> str:
    """Substitute __NAME__ placeholders in a remote shell script template."""
    for key, value in subs.items():
        script = script.replace(f"__{key}__", value)
    return script


def _fail(op: dict[str, Any], message: str) -> None:
    """Mark an operation failed with a message."""
    op["passed"] = False
    op["error"] = message


def main() -> int:
    """Create, attach, format, mount, and seed a block volume; print JSON."""
    parser = argparse.ArgumentParser(description="Block-storage fixture: create + attach + seed a volume")
    parser.add_argument("--instance-id", required=True, help="EC2 instance to attach the volume to")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--size-gib", type=int, default=10, help="Volume size in GiB")
    parser.add_argument("--device", default="/dev/sdf", help="Requested attach device name")
    parser.add_argument("--mount-point", default="/mnt/isv-block", help="In-guest mount point")
    args = parser.parse_args()

    sentinel_content = f"isv-ncp-validate-storage-{uuid.uuid4().hex}"
    operations: dict[str, dict[str, Any]] = {
        "create": {"passed": False},
        "attach": {"passed": False},
        "format": {"passed": False},
        "mount": {"passed": False},
        "write_sentinel": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "create_volume",
        "instance_id": args.instance_id,
        "volume_id": None,
        "device": args.device,
        "mount_point": args.mount_point,
        "size_gib": args.size_gib,
        "sentinel_path": f"{args.mount_point}/isv-sentinel.txt",
        "sentinel_content": sentinel_content,
        "operations": operations,
    }

    ec2 = boto3.client("ec2", region_name=args.region)

    try:
        instance = ebs.describe_instance(ec2, args.instance_id)
        availability_zone = instance["Placement"]["AvailabilityZone"]
        public_ip = instance.get("PublicIpAddress")
        result["availability_zone"] = availability_zone

        volume_id = ebs.create_volume(ec2, availability_zone, args.size_gib, name="isv-validate-block")
        result["volume_id"] = volume_id
        ebs.wait_for_volume_available(ec2, volume_id)
        operations["create"]["passed"] = True

        ebs.attach_volume(ec2, volume_id, args.instance_id, args.device)
        ebs.wait_for_volume_in_use(ec2, volume_id)
        operations["attach"]["passed"] = True
    except (ClientError, BotoCoreError, RuntimeError) as e:
        result["error"] = f"Volume create/attach failed: {e}"
        print(json.dumps(result, indent=2))
        return 1

    if not public_ip:
        result["error"] = "Instance has no public IP for SSH"
        print(json.dumps(result, indent=2))
        return 1

    if not wait_for_ssh(public_ip, args.ssh_user, args.key_file, max_attempts=30, interval=10):
        result["error"] = "SSH not ready on fixture instance"
        print(json.dumps(result, indent=2))
        return 1

    if not ebs.wait_for_attachment_device(public_ip, args.ssh_user, args.key_file, volume_id):
        _fail(operations["format"], "Attached volume did not appear in guest")
        result["error"] = "Attached volume device never appeared in guest"
        print(json.dumps(result, indent=2))
        return 1

    setup = _render(
        _SETUP_SCRIPT,
        BYID=ebs.guest_by_id_path(volume_id),
        MOUNT=args.mount_point,
        CONTENT=sentinel_content,
    )
    rc, _, err = ssh_run(public_ip, args.ssh_user, args.key_file, setup, timeout=180)
    if rc == 0:
        operations["format"]["passed"] = True
        operations["mount"]["passed"] = True
        operations["write_sentinel"]["passed"] = True
    else:
        _fail(operations["format"], f"Guest format/mount/seed failed (rc={rc}): {err.strip()[:300]}")
        result["error"] = "Guest format/mount/seed failed"

    result["success"] = all(op["passed"] for op in operations.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
