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

"""Launch AWS EC2 bare-metal GPU instance for BMaaS testing.

Similar to the VM launch script but targeted at bare-metal instance types
(e.g. g4dn.metal, p4de.24xlarge, p5.48xlarge) which provide direct hardware
access without a hypervisor.

Reuses shared helpers from common/ec2.py for key pairs, security groups,
and AZ-aware subnet selection.

Instance reuse (dev workflow):
    Set AWS_BM_INSTANCE_ID and AWS_BM_KEY_FILE env vars to skip launching
    and describe an existing instance instead. This allows fast iteration
    on validations without reprovisioning.

Usage:
    python launch_instance.py --name isv-bm-test --instance-type g4dn.metal --region us-west-2

    # Reuse existing instance:
    AWS_BM_INSTANCE_ID=i-xxx AWS_BM_KEY_FILE=/tmp/key.pem python launch_instance.py

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "instance_type": "g4dn.metal",
    "public_ip": "54.x.x.x",
    "private_ip": "10.0.1.5",
    "state": "running",
    "ami_id": "ami-xxx",
    "key_name": "isv-bm-test-key",
    "key_file": "/tmp/isv-bm-test-key.pem"
}
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import (
    create_key_pair,
    create_security_group,
    get_architecture_for_instance_type,
    get_default_vpc_and_subnets,
)


def get_bare_metal_ami(ec2: Any, instance_type: str) -> str | None:
    """Get appropriate AMI for a bare-metal GPU instance.

    Bare-metal instances need AMIs that boot directly on hardware.
    Deep Learning AMIs with NVIDIA drivers pre-installed are preferred.

    Args:
        ec2: boto3 EC2 client
        instance_type: EC2 instance type (used to detect architecture)

    Returns:
        AMI ID or None if not found
    """
    architecture = get_architecture_for_instance_type(instance_type)

    ami_patterns = [
        "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*",
        "Deep Learning Base GPU AMI (Ubuntu 22.04)*",
        "Deep Learning AMI GPU PyTorch*Ubuntu 22.04*",
    ]
    fallback_pattern = (
        "ubuntu/images/hvm-ssd-gp3/ubuntu-jammy-22.04-arm64-server-*"
        if architecture == "arm64"
        else "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
    )

    for pattern in ami_patterns:
        response = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": [pattern]},
                {"Name": "state", "Values": ["available"]},
                {"Name": "architecture", "Values": [architecture]},
            ],
        )
        images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
        if images:
            return images[0]["ImageId"]

    response = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": [fallback_pattern]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
    return images[0]["ImageId"] if images else None


def reuse_existing_instance(region: str) -> int:
    """Describe an existing instance instead of launching a new one.

    Used when AWS_BM_INSTANCE_ID and AWS_BM_KEY_FILE are set.

    Args:
        region: AWS region for the EC2 client.

    Returns:
        0 on success, 1 on failure
    """
    instance_id = os.environ["AWS_BM_INSTANCE_ID"]
    key_file = os.environ["AWS_BM_KEY_FILE"]

    print(f"Reusing existing instance {instance_id}", file=sys.stderr)

    ec2 = boto3.client("ec2", region_name=region)
    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": instance_id,
        "region": region,
        "key_file": key_file,
        "reused": True,
    }

    try:
        instances = ec2.describe_instances(InstanceIds=[instance_id])
        instance = instances["Reservations"][0]["Instances"][0]

        result["state"] = instance["State"]["Name"]
        result["instance_type"] = instance.get("InstanceType")
        result["public_ip"] = instance.get("PublicIpAddress")
        result["private_ip"] = instance.get("PrivateIpAddress")
        result["vpc_id"] = instance.get("VpcId")
        result["subnet_id"] = instance.get("SubnetId")
        result["key_name"] = instance.get("KeyName")
        result["availability_zone"] = instance.get("Placement", {}).get("AvailabilityZone")
        result["success"] = result["state"] == "running"

        if not result["success"]:
            result["error"] = f"Instance {instance_id} is {result['state']}, expected running"
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


def main() -> int:
    """Launch a bare-metal GPU EC2 instance for BMaaS testing.

    If AWS_BM_INSTANCE_ID and AWS_BM_KEY_FILE are set, skips launching
    and describes the existing instance instead (dev workflow).

    Otherwise, creates key pair + security group, selects an appropriate AMI,
    and launches the instance with subnet fallback logic. Waits for
    the instance to pass both running and status-ok checks (bare-metal
    instances take longer to fully boot).

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Launch bare-metal GPU instance")
    parser.add_argument("--name", default="isv-bm-test-gpu", help="Instance name")
    parser.add_argument("--instance-type", default="g4dn.metal", help="EC2 bare-metal instance type")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--vpc-id", help="VPC ID (uses default if not specified)")
    parser.add_argument("--subnet-id", help="Subnet ID (uses first subnet if not specified)")
    parser.add_argument("--ami-id", help="AMI ID (auto-detects GPU AMI if not specified)")
    parser.add_argument("--key-name", default="isv-bm-test-key", help="EC2 key pair name")
    parser.add_argument(
        "--volume-size",
        type=int,
        default=200,
        help="Root volume size in GiB (default: 200, larger for BM workloads)",
    )
    args = parser.parse_args()

    # Reuse existing instance if env vars are set
    if os.environ.get("AWS_BM_INSTANCE_ID") and os.environ.get("AWS_BM_KEY_FILE"):
        return reuse_existing_instance(args.region)

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": None,
        "instance_type": args.instance_type,
        "region": args.region,
    }

    try:
        if args.vpc_id and args.subnet_id:
            vpc_id = args.vpc_id
            subnet_list = [args.subnet_id]
        else:
            vpc_id, subnet_list = get_default_vpc_and_subnets(ec2, args.instance_type)

        key_file = create_key_pair(ec2, args.key_name)
        result["key_name"] = args.key_name
        result["key_file"] = key_file

        sg_name = f"{args.name}-sg"
        sg_id = create_security_group(ec2, vpc_id, sg_name)
        result["security_group_id"] = sg_id

        architecture = get_architecture_for_instance_type(args.instance_type)
        result["architecture"] = architecture

        ami_id = args.ami_id or get_bare_metal_ami(ec2, args.instance_type)
        if not ami_id:
            raise RuntimeError(f"Could not find suitable {architecture} AMI for bare-metal")
        result["ami_id"] = ami_id

        ami_info = ec2.describe_images(ImageIds=[ami_id])
        if ami_info["Images"]:
            result["ami_name"] = ami_info["Images"][0].get("Name", "unknown")
            result["ami_architecture"] = ami_info["Images"][0].get("Architecture", "unknown")

        last_error = None
        instance_id = None

        for subnet_id in subnet_list:
            try:
                response = ec2.run_instances(
                    ImageId=ami_id,
                    InstanceType=args.instance_type,
                    MinCount=1,
                    MaxCount=1,
                    KeyName=args.key_name,
                    SubnetId=subnet_id,
                    SecurityGroupIds=[sg_id],
                    TagSpecifications=[
                        {
                            "ResourceType": "instance",
                            "Tags": [
                                {"Key": "Name", "Value": args.name},
                                {"Key": "Platform", "Value": "bare-metal"},
                                {"Key": "CreatedBy", "Value": "isvtest"},
                            ],
                        }
                    ],
                    BlockDeviceMappings=[
                        {
                            "DeviceName": "/dev/sda1",
                            "Ebs": {
                                "VolumeSize": args.volume_size,
                                "VolumeType": "gp3",
                            },
                        }
                    ],
                )
                instance_id = response["Instances"][0]["InstanceId"]
                result["subnet_id"] = subnet_id
                break
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code in ("Unsupported", "InsufficientInstanceCapacity"):
                    last_error = e
                    continue
                raise

        if not instance_id:
            if last_error:
                raise last_error
            raise RuntimeError("Failed to launch bare-metal instance in any subnet")

        result["instance_id"] = instance_id

        # Bare-metal instances take longer to boot; use generous waiter config
        print("Waiting for instance to reach running state...", file=sys.stderr)
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 60},
        )

        print("Waiting for instance status checks (bare-metal takes longer)...", file=sys.stderr)
        status_waiter = ec2.get_waiter("instance_status_ok")
        status_waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )

        instances = ec2.describe_instances(InstanceIds=[instance_id])
        instance = instances["Reservations"][0]["Instances"][0]

        result["public_ip"] = instance.get("PublicIpAddress")
        result["private_ip"] = instance.get("PrivateIpAddress")
        result["state"] = instance["State"]["Name"]
        result["vpc_id"] = vpc_id
        result["availability_zone"] = instance.get("Placement", {}).get("AvailabilityZone")
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
