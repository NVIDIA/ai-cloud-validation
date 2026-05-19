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

"""Reinstall a bare-metal EC2 instance from its configured stock OS.

AWS does not support CreateReplaceRootVolumeTask on metal instances, so
this script performs the equivalent manually:
  1. Get the original AMI's root snapshot
  2. Stop the instance
  3. Detach the current root volume
  4. Create a new volume from the AMI snapshot
  5. Attach the new volume as root
  6. Start the instance
  7. Wait for status checks + SSH
  8. Delete old root volume (post-success cleanup)

Usage:
    python reinstall_instance.py --instance-id i-xxx --region us-west-2 \
        --key-file /tmp/key.pem

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "state": "running",
    "public_ip": "54.x.x.x",
    "key_file": "/tmp/key.pem",
    "ssh_user": "ubuntu",
    "ssh_ready": true,
    "reinstall_method": "root_volume_swap"
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
from botocore.exceptions import ClientError, WaiterError
from common.ec2 import wait_for_public_ip
from common.ssh_utils import wait_for_ssh


def get_ami_root_snapshot(ec2: Any, ami_id: str) -> tuple[str, str]:
    """Get the root device snapshot ID and device name from an AMI.

    Args:
        ec2: boto3 EC2 client
        ami_id: AMI ID to inspect

    Returns:
        Tuple of (snapshot_id, device_name)

    Raises:
        RuntimeError: If AMI not found or has no root snapshot
    """
    images = ec2.describe_images(ImageIds=[ami_id])
    if not images["Images"]:
        raise RuntimeError(f"AMI {ami_id} not found")

    image = images["Images"][0]
    root_device = image["RootDeviceName"]

    for bdm in image.get("BlockDeviceMappings", []):
        if bdm.get("DeviceName") == root_device and "Ebs" in bdm:
            return bdm["Ebs"]["SnapshotId"], root_device

    raise RuntimeError(f"No root snapshot found in AMI {ami_id}")


def main() -> int:
    """Reinstall a bare-metal instance by swapping its root volume.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Reinstall bare-metal instance from stock OS")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--ami-id", help="AMI ID to reinstall from (default: instance's current AMI)")
    parser.add_argument(
        "--volume-size",
        type=int,
        default=200,
        help="New root volume size in GiB (default: 200)",
    )
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "region": args.region,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "ssh_ready": False,
        "reinstall_method": "root_volume_swap",
    }

    old_volume_id = None

    try:
        # Step 1: Get instance details
        print("Getting instance details...", file=sys.stderr)
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]

        if instance["State"]["Name"] != "running":
            result["error"] = f"Instance is {instance['State']['Name']}, expected running"
            print(json.dumps(result, indent=2))
            return 1

        ami_id = args.ami_id or instance.get("ImageId")
        if not ami_id:
            result["error"] = "Cannot determine AMI ID for reinstall"
            print(json.dumps(result, indent=2))
            return 1

        result["ami_id"] = ami_id
        az = instance["Placement"]["AvailabilityZone"]
        root_device = instance.get("RootDeviceName", "/dev/sda1")

        # Find current root volume
        for bdm in instance.get("BlockDeviceMappings", []):
            if bdm.get("DeviceName") == root_device:
                old_volume_id = bdm["Ebs"]["VolumeId"]
                break

        if not old_volume_id:
            result["error"] = f"Cannot find root volume for device {root_device}"
            print(json.dumps(result, indent=2))
            return 1

        print(f"  AMI: {ami_id}, Root device: {root_device}, Old volume: {old_volume_id}", file=sys.stderr)

        # Step 2: Get AMI's root snapshot
        print("Getting AMI root snapshot...", file=sys.stderr)
        snapshot_id, _ = get_ami_root_snapshot(ec2, ami_id)
        print(f"  Snapshot: {snapshot_id}", file=sys.stderr)

        # Step 3: Stop the instance (bare-metal can take 15-20+ min)
        print(f"Stopping instance {args.instance_id}...", file=sys.stderr)
        ec2.stop_instances(InstanceIds=[args.instance_id])

        waiter = ec2.get_waiter("instance_stopped")
        try:
            waiter.wait(
                InstanceIds=[args.instance_id],
                WaiterConfig={"Delay": 30, "MaxAttempts": 50},
            )
        except WaiterError:
            # Check if it actually stopped despite waiter timeout
            inst = ec2.describe_instances(InstanceIds=[args.instance_id])
            state = inst["Reservations"][0]["Instances"][0]["State"]["Name"]
            if state != "stopped":
                raise RuntimeError(
                    f"Instance failed to stop (state: {state}). Bare-metal instances can take 20+ min to stop."
                )
        print("  Instance stopped", file=sys.stderr)

        # Step 4: Detach old root volume
        print(f"Detaching old root volume {old_volume_id}...", file=sys.stderr)
        ec2.detach_volume(VolumeId=old_volume_id, InstanceId=args.instance_id, Force=True)
        vol_waiter = ec2.get_waiter("volume_available")
        vol_waiter.wait(VolumeIds=[old_volume_id])
        print("  Old volume detached", file=sys.stderr)

        # Step 5: Create new volume from AMI snapshot
        print(f"Creating new root volume from snapshot {snapshot_id}...", file=sys.stderr)
        new_volume = ec2.create_volume(
            SnapshotId=snapshot_id,
            AvailabilityZone=az,
            VolumeType="gp3",
            Size=args.volume_size,
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": "Name", "Value": f"reinstall-{args.instance_id}"},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        new_volume_id = new_volume["VolumeId"]
        result["new_volume_id"] = new_volume_id
        vol_waiter.wait(VolumeIds=[new_volume_id])
        print(f"  New volume created: {new_volume_id}", file=sys.stderr)

        # Step 6: Attach new volume as root
        print(f"Attaching new volume as {root_device}...", file=sys.stderr)
        ec2.attach_volume(
            VolumeId=new_volume_id,
            InstanceId=args.instance_id,
            Device=root_device,
        )
        attach_waiter = ec2.get_waiter("volume_in_use")
        attach_waiter.wait(VolumeIds=[new_volume_id])
        print("  New volume attached", file=sys.stderr)

        # Step 7: Start the instance
        print(f"Starting instance {args.instance_id}...", file=sys.stderr)
        ec2.start_instances(InstanceIds=[args.instance_id])

        run_waiter = ec2.get_waiter("instance_running")
        run_waiter.wait(
            InstanceIds=[args.instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 60},
        )

        print("Waiting for instance status checks...", file=sys.stderr)
        status_waiter = ec2.get_waiter("instance_status_ok")
        status_waiter.wait(
            InstanceIds=[args.instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )
        print("  Instance status checks passed", file=sys.stderr)

        # Step 8: Get updated instance details
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]

        result["state"] = instance["State"]["Name"]
        result["private_ip"] = instance.get("PrivateIpAddress")

        # Poll for the fresh public IP; do not fall back to a caller-supplied
        # value - IPs are released on stop on NCPs and would be stale.
        public_ip = instance.get("PublicIpAddress") or wait_for_public_ip(ec2, args.instance_id)
        if not public_ip:
            result["error"] = "Instance has no public IP after reinstall (timed out polling)"
            print(json.dumps(result, indent=2))
            return 1
        result["public_ip"] = public_ip

        # Step 9: Wait for SSH
        print("Waiting for SSH after reinstall...", file=sys.stderr)
        ssh_ready = wait_for_ssh(public_ip, args.ssh_user, args.key_file)
        result["ssh_ready"] = ssh_ready

        if not ssh_ready:
            result["error"] = "SSH not ready after reinstall"
            print(json.dumps(result, indent=2))
            return 1

        result["success"] = True
        print("Reinstall completed successfully!", file=sys.stderr)

        # Step 10: Clean up old root volume (post-success only)
        if old_volume_id:
            print(f"Cleaning up old volume {old_volume_id}...", file=sys.stderr)
            try:
                ec2.delete_volume(VolumeId=old_volume_id)
                print("  Old volume deleted", file=sys.stderr)
            except ClientError as e:
                print(f"  Warning: could not delete old volume: {e}", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
