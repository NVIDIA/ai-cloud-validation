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
  2. Create a new volume from the AMI snapshot, or from a temporary donor instance
  3. Stop the instance
  4. Detach the current root volume
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
from common.errors import delete_with_retry
from common.ssh_utils import ssh_run, wait_for_ssh


def get_ami_root_snapshot(ec2: Any, ami_id: str) -> tuple[str, str, str]:
    """Get the root device snapshot ID and device name from an AMI.

    Args:
        ec2: boto3 EC2 client
        ami_id: AMI ID to inspect

    Returns:
        Tuple of (snapshot_id, device_name, architecture)

    Raises:
        RuntimeError: If AMI not found or has no root snapshot
    """
    images = ec2.describe_images(ImageIds=[ami_id])
    if not images["Images"]:
        raise RuntimeError(f"AMI {ami_id} not found")

    image = images["Images"][0]
    root_device = image["RootDeviceName"]
    architecture = image.get("Architecture", "x86_64")

    for bdm in image.get("BlockDeviceMappings", []):
        if bdm.get("DeviceName") == root_device and "Ebs" in bdm:
            return bdm["Ebs"]["SnapshotId"], root_device, architecture

    raise RuntimeError(f"No root snapshot found in AMI {ami_id}")


def get_root_volume_id(instance: dict[str, Any], root_device: str) -> str | None:
    """Return the volume ID attached to an instance root device."""
    for bdm in instance.get("BlockDeviceMappings", []):
        if bdm.get("DeviceName") == root_device:
            return bdm["Ebs"]["VolumeId"]
    return None


def get_donor_instance_type(architecture: str) -> str:
    """Return a small donor instance type that matches an AMI architecture."""
    # Match any arm variant (arm64, arm64_mac, ...) to a Graviton type; everything
    # else (x86_64, i386) gets the x86 type. Avoids launching an x86 donor against
    # an arm AMI, which RunInstances rejects.
    return "t4g.micro" if architecture.startswith("arm") else "t3.micro"


def create_root_volume_from_snapshot(
    ec2: Any,
    snapshot_id: str,
    availability_zone: str,
    volume_size: int,
    instance_id: str,
) -> str:
    """Create a replacement root volume from an AMI snapshot."""
    new_volume = ec2.create_volume(
        SnapshotId=snapshot_id,
        AvailabilityZone=availability_zone,
        VolumeType="gp3",
        Size=volume_size,
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "Name", "Value": f"reinstall-{instance_id}"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            }
        ],
    )
    new_volume_id = new_volume["VolumeId"]
    try:
        vol_waiter = ec2.get_waiter("volume_available")
        vol_waiter.wait(VolumeIds=[new_volume_id])
    except Exception:
        # The volume exists but never became available; delete it before
        # propagating so a transient waiter failure doesn't orphan it.
        delete_with_retry(
            ec2.delete_volume,
            VolumeId=new_volume_id,
            resource_desc=f"replacement root volume {new_volume_id}",
        )
        raise
    return new_volume_id


def create_root_volume_from_donor_instance(
    ec2: Any,
    *,
    ami_id: str,
    image_root_device: str,
    architecture: str,
    volume_size: int,
    subnet_id: str | None,
    security_group_ids: list[str],
    key_name: str | None,
    instance_id: str,
) -> str:
    """Launch a temporary instance and detach its root volume as the replacement."""
    if not subnet_id:
        raise RuntimeError("Cannot create donor root volume: target instance has no subnet ID")

    donor_instance_id = None
    donor_volume_id = None
    # True only between issuing the donor detach and the volume becoming
    # available: a failure in that window orphans a detached volume that
    # terminate's DeleteOnTermination no longer covers, so cleanup deletes it.
    donor_volume_detach_pending = False
    run_args: dict[str, Any] = {
        "ImageId": ami_id,
        "InstanceType": get_donor_instance_type(architecture),
        "MinCount": 1,
        "MaxCount": 1,
        "SubnetId": subnet_id,
        "BlockDeviceMappings": [
            {
                "DeviceName": image_root_device,
                "Ebs": {
                    "VolumeSize": volume_size,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"reinstall-donor-{instance_id}"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            },
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "Name", "Value": f"reinstall-{instance_id}"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            },
        ],
    }
    if security_group_ids:
        run_args["SecurityGroupIds"] = security_group_ids
    if key_name:
        run_args["KeyName"] = key_name

    try:
        donor = ec2.run_instances(**run_args)
        donor_instance_id = donor["Instances"][0]["InstanceId"]

        run_waiter = ec2.get_waiter("instance_running")
        run_waiter.wait(
            InstanceIds=[donor_instance_id],
            WaiterConfig={"Delay": 10, "MaxAttempts": 60},
        )

        ec2.stop_instances(InstanceIds=[donor_instance_id])
        stop_waiter = ec2.get_waiter("instance_stopped")
        stop_waiter.wait(
            InstanceIds=[donor_instance_id],
            WaiterConfig={"Delay": 10, "MaxAttempts": 60},
        )

        donor_details = ec2.describe_instances(InstanceIds=[donor_instance_id])
        donor_instance = donor_details["Reservations"][0]["Instances"][0]
        donor_volume_id = get_root_volume_id(donor_instance, image_root_device)
        if not donor_volume_id:
            raise RuntimeError(f"Cannot find donor root volume for device {image_root_device}")

        ec2.detach_volume(VolumeId=donor_volume_id, InstanceId=donor_instance_id, Force=True)
        donor_volume_detach_pending = True
        vol_waiter = ec2.get_waiter("volume_available")
        vol_waiter.wait(VolumeIds=[donor_volume_id])
        donor_volume_detach_pending = False
        return donor_volume_id
    finally:
        if donor_instance_id:
            try:
                ec2.terminate_instances(InstanceIds=[donor_instance_id])
            except ClientError as e:
                print(f"  Warning: could not terminate donor instance {donor_instance_id}: {e}", file=sys.stderr)
        if donor_volume_id and donor_volume_detach_pending:
            deleted = delete_with_retry(
                ec2.delete_volume,
                VolumeId=donor_volume_id,
                resource_desc=f"donor root volume {donor_volume_id}",
            )
            if not deleted:
                print(f"  Warning: could not delete donor root volume {donor_volume_id}", file=sys.stderr)


def create_replacement_root_volume(
    ec2: Any,
    *,
    ami_id: str,
    snapshot_id: str,
    image_root_device: str,
    architecture: str,
    availability_zone: str,
    volume_size: int,
    subnet_id: str | None,
    security_group_ids: list[str],
    key_name: str | None,
    instance_id: str,
) -> str:
    """Create the replacement root volume before mutating the target instance."""
    print(f"Creating new root volume from snapshot {snapshot_id}...", file=sys.stderr)
    try:
        return create_root_volume_from_snapshot(ec2, snapshot_id, availability_zone, volume_size, instance_id)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code")
        if error_code != "InvalidSnapshot.NotFound":
            raise

        print(
            "  AMI snapshot is not directly restorable; creating root volume from a temporary donor instance...",
            file=sys.stderr,
        )
        return create_root_volume_from_donor_instance(
            ec2,
            ami_id=ami_id,
            image_root_device=image_root_device,
            architecture=architecture,
            volume_size=volume_size,
            subnet_id=subnet_id,
            security_group_ids=security_group_ids,
            key_name=key_name,
            instance_id=instance_id,
        )


def query_node_instance_id(host: str, user: str, key_file: str) -> str:
    """Read the instance ID reported by the reinstalled OS."""
    command = (
        "TOKEN=$(curl -sf -X PUT 'http://169.254.169.254/latest/api/token' "
        "-H 'X-aws-ec2-metadata-token-ttl-seconds: 60') && "
        'curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" '
        "http://169.254.169.254/latest/meta-data/instance-id"
    )
    exit_code, stdout, _ = ssh_run(host, user, key_file, command)
    return stdout.strip() if exit_code == 0 else ""


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
    new_volume_id = None

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
        subnet_id = instance.get("SubnetId")
        security_group_ids = [sg["GroupId"] for sg in instance.get("SecurityGroups", []) if sg.get("GroupId")]
        key_name = instance.get("KeyName")

        old_volume_id = get_root_volume_id(instance, root_device)

        if not old_volume_id:
            result["error"] = f"Cannot find root volume for device {root_device}"
            print(json.dumps(result, indent=2))
            return 1

        print(f"  AMI: {ami_id}, Root device: {root_device}, Old volume: {old_volume_id}", file=sys.stderr)

        # Step 2: Get AMI's root snapshot
        print("Getting AMI root snapshot...", file=sys.stderr)
        snapshot_id, image_root_device, architecture = get_ami_root_snapshot(ec2, ami_id)
        print(f"  Snapshot: {snapshot_id}", file=sys.stderr)

        # Step 3: Create the replacement root volume before mutating the target instance.
        new_volume_id = create_replacement_root_volume(
            ec2,
            ami_id=ami_id,
            snapshot_id=snapshot_id,
            image_root_device=image_root_device,
            architecture=architecture,
            availability_zone=az,
            volume_size=args.volume_size,
            subnet_id=subnet_id,
            security_group_ids=security_group_ids,
            key_name=key_name,
            instance_id=args.instance_id,
        )
        result["new_volume_id"] = new_volume_id
        print(f"  New volume created: {new_volume_id}", file=sys.stderr)

        # Step 4: Stop the instance (bare-metal can take 15-20+ min)
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

        # Step 5: Detach old root volume
        print(f"Detaching old root volume {old_volume_id}...", file=sys.stderr)
        ec2.detach_volume(VolumeId=old_volume_id, InstanceId=args.instance_id, Force=True)
        vol_waiter = ec2.get_waiter("volume_available")
        vol_waiter.wait(VolumeIds=[old_volume_id])
        print("  Old volume detached", file=sys.stderr)

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
            raise RuntimeError("Instance has no public IP after reinstall (timed out polling)")
        result["public_ip"] = public_ip

        # Step 9: Wait for SSH
        print("Waiting for SSH after reinstall...", file=sys.stderr)
        ssh_ready = wait_for_ssh(public_ip, args.ssh_user, args.key_file)
        result["ssh_ready"] = ssh_ready

        if not ssh_ready:
            raise RuntimeError("SSH not ready after reinstall")

        # Report the node-observed identity (empty if it could not be read) and
        # let StableIdentifierCheck do the comparison. The reinstall itself has
        # succeeded by this point, so a metadata-read hiccup must not abort the
        # step (which would skip downstream checks and leak the old root volume).
        result["instance_id"] = query_node_instance_id(public_ip, args.ssh_user, args.key_file)

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

    # On failure after the replacement volume was created, delete it. Teardown
    # only terminates the instance and never reclaims reinstall-* volumes, so a
    # failure between volume creation and a successful swap would otherwise
    # orphan the volume (in-use volumes log a warning rather than being deleted).
    if not result["success"] and new_volume_id:
        print(f"Cleaning up replacement volume {new_volume_id} after failure...", file=sys.stderr)
        deleted = delete_with_retry(
            ec2.delete_volume,
            VolumeId=new_volume_id,
            resource_desc=f"replacement root volume {new_volume_id}",
        )
        if not deleted:
            print(f"  Warning: could not delete replacement volume {new_volume_id}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
