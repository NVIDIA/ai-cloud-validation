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

"""Launch AWS EC2 GPU instance for VM testing.

Usage:
    python launch_instance.py --name test-gpu --instance-type g5.xlarge --region us-west-2

Output JSON:
{
    "success": true,
    "instance_id": "i-xxx",
    "instance_type": "g5.xlarge",
    "public_ip": "54.x.x.x",
    "private_ip": "10.0.1.5",
    "state": "running",
    "ami_id": "ami-xxx",
    "requested_key_name": "isv-test-key",
    "key_name": "isv-test-key",
    "key_file": "/tmp/isv-test-key.pem"
}
"""

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import (
    create_key_pair,
    create_security_group,
    get_architecture_for_instance_type,
    get_default_vpc_and_subnets,
)


def add_specified_key_contract(
    result: dict[str, Any],
    *,
    requested_key_name: str | None,
    actual_key_name: str | None,
) -> None:
    """Add provider-neutral specified-key evidence to launch output."""
    key_matches = bool(requested_key_name) and actual_key_name == requested_key_name

    result["requested_key_name"] = requested_key_name or ""
    result["key_name"] = actual_key_name or ""
    result.setdefault("tests", {})["specified_key"] = {
        "passed": key_matches,
        "message": (
            f"Instance uses requested key '{requested_key_name}'"
            if key_matches
            else f"Instance expected key '{requested_key_name or ''}', got '{actual_key_name or ''}'"
        ),
        "probes": ["instance_key_name"],
    }


def get_gpu_ami(ec2: Any, instance_type: str) -> str | None:
    """Get appropriate AMI for GPU instance with NVIDIA drivers pre-installed.

    Selects AMI based on instance type architecture (x86_64 vs arm64).

    Args:
        ec2: boto3 EC2 client
        instance_type: EC2 instance type (used to detect architecture)

    Returns:
        AMI ID or None if not found
    """
    architecture = get_architecture_for_instance_type(instance_type)

    # AMI search patterns by architecture
    if architecture == "arm64":
        ami_patterns = [
            "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*",
            "Deep Learning AMI Graviton GPU PyTorch*Ubuntu 22.04*",
            "Deep Learning Base AMI GPU Graviton*Ubuntu 22.04*",
        ]
        fallback_pattern = "ubuntu/images/hvm-ssd-gp3/ubuntu-jammy-22.04-arm64-server-*"
    else:
        ami_patterns = [
            "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*",
            "Deep Learning Base GPU AMI (Ubuntu 22.04)*",
            "Deep Learning AMI GPU PyTorch*Ubuntu 22.04*",
            "Deep Learning AMI GPU PyTorch*Ubuntu 20.04*",
            "Deep Learning Base AMI (Ubuntu 20.04)*",
        ]
        fallback_pattern = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"

    # Search for Deep Learning AMIs
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

    # Last resort: plain Ubuntu (will NOT have GPU drivers)
    response = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": [fallback_pattern]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
    return images[0]["ImageId"] if images else None


def reuse_existing_instance(region: str, requested_key_name: str) -> int:
    """Describe an existing instance instead of launching a new one.

    Used when AWS_VM_INSTANCE_ID and AWS_VM_KEY_FILE are set.

    Args:
        region: AWS region for the EC2 client.
        requested_key_name: Key name the launch contract expects.

    Returns:
        0 on success, 1 on failure
    """
    instance_id = os.environ["AWS_VM_INSTANCE_ID"]
    key_file = os.environ["AWS_VM_KEY_FILE"]

    print(f"Reusing existing instance {instance_id}", file=sys.stderr)

    ec2 = boto3.client("ec2", region_name=region)
    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": instance_id,
        "region": region,
        "key_file": key_file,
        "reused": True,
    }

    try:
        instances = ec2.describe_instances(InstanceIds=[instance_id])
        instance = instances["Reservations"][0]["Instances"][0]

        state = instance["State"]["Name"]

        # Start the instance if it's stopped
        if state == "stopped":
            print(f"  Instance {instance_id} is stopped - starting it...", file=sys.stderr)
            ec2.start_instances(InstanceIds=[instance_id])
            waiter = ec2.get_waiter("instance_status_ok")
            waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
            instances = ec2.describe_instances(InstanceIds=[instance_id])
            instance = instances["Reservations"][0]["Instances"][0]
            state = instance["State"]["Name"]
            print(f"  Instance {instance_id} is now {state}", file=sys.stderr)

        result["state"] = state
        result["instance_type"] = instance.get("InstanceType")
        result["public_ip"] = instance.get("PublicIpAddress")
        result["private_ip"] = instance.get("PrivateIpAddress")
        result["vpc_id"] = instance.get("VpcId")
        result["subnet_id"] = instance.get("SubnetId")
        result["security_group_id"] = (instance.get("SecurityGroups") or [{}])[0].get("GroupId")
        result["availability_zone"] = instance.get("Placement", {}).get("AvailabilityZone")
        add_specified_key_contract(
            result,
            requested_key_name=requested_key_name,
            actual_key_name=instance.get("KeyName"),
        )
        result["success"] = state == "running"

        if not result["success"]:
            result["error"] = f"Instance {instance_id} is {state}, expected running"
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


def main() -> int:
    """Launch a GPU-enabled EC2 instance for VM testing.

    If AWS_VM_INSTANCE_ID and AWS_VM_KEY_FILE are set, skips launching
    and describes the existing instance instead (dev workflow).

    Otherwise, parses command-line arguments, creates necessary resources
    (key pair, security group), selects an appropriate AMI based on instance
    type architecture, and launches the instance with fallback subnet logic.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Launch GPU instance")
    parser.add_argument("--name", default="isv-test-gpu", help="Instance name")
    parser.add_argument("--instance-type", default="g5.xlarge", help="EC2 instance type")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--vpc-id", help="VPC ID (uses default if not specified)")
    parser.add_argument("--subnet-id", help="Subnet ID (uses first subnet if not specified)")
    parser.add_argument("--ami-id", help="AMI ID (auto-detects GPU AMI if not specified)")
    parser.add_argument("--key-name", default="isv-test-key", help="EC2 key pair name")
    args = parser.parse_args()

    # Reuse existing instance if env vars are set
    if os.environ.get("AWS_VM_INSTANCE_ID") and os.environ.get("AWS_VM_KEY_FILE"):
        return reuse_existing_instance(args.region, args.key_name)

    ec2 = boto3.client("ec2", region_name=args.region)

    result = {
        "success": False,
        "platform": "vm",
        "instance_id": None,
        "instance_type": args.instance_type,
        "region": args.region,
    }

    try:
        # Get VPC and subnets
        if args.vpc_id and args.subnet_id:
            vpc_id = args.vpc_id
            subnet_list = [args.subnet_id]
        else:
            vpc_id, subnet_list = get_default_vpc_and_subnets(ec2, args.instance_type)

        # Create key pair
        key_file = create_key_pair(ec2, args.key_name)
        result["key_file"] = key_file

        # Create security group
        sg_name = f"{args.name}-sg"
        sg_id = create_security_group(ec2, vpc_id, sg_name)
        result["security_group_id"] = sg_id

        # Get AMI (architecture-aware selection based on instance type)
        architecture = get_architecture_for_instance_type(args.instance_type)
        result["architecture"] = architecture

        ami_id = args.ami_id or get_gpu_ami(ec2, args.instance_type)
        if not ami_id:
            raise RuntimeError(f"Could not find suitable {architecture} AMI")
        result["ami_id"] = ami_id

        # Get AMI name for logging
        ami_info = ec2.describe_images(ImageIds=[ami_id])
        if ami_info["Images"]:
            result["ami_name"] = ami_info["Images"][0].get("Name", "unknown")
            result["ami_architecture"] = ami_info["Images"][0].get("Architecture", "unknown")

        # Try launching in each subnet until one succeeds
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
                                {"Key": "CreatedBy", "Value": "isvtest"},
                            ],
                        }
                    ],
                    BlockDeviceMappings=[
                        {
                            "DeviceName": "/dev/sda1",
                            "Ebs": {"VolumeSize": 100, "VolumeType": "gp3"},
                        }
                    ],
                )
                instance_id = response["Instances"][0]["InstanceId"]
                result["subnet_id"] = subnet_id
                break  # Success, exit loop
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "Unsupported":
                    # AZ doesn't support instance type, try next subnet
                    last_error = e
                    continue
                raise  # Other errors should be raised

        if not instance_id:
            if last_error:
                raise last_error
            raise RuntimeError("Failed to launch instance in any subnet")

        result["instance_id"] = instance_id

        # Wait for instance to be running
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        # Wait for instance status checks to pass (ensures OS is ready)
        status_waiter = ec2.get_waiter("instance_status_ok")
        status_waiter.wait(InstanceIds=[instance_id])

        # Get instance details
        instances = ec2.describe_instances(InstanceIds=[instance_id])
        instance = instances["Reservations"][0]["Instances"][0]

        result["public_ip"] = instance.get("PublicIpAddress")
        result["private_ip"] = instance.get("PrivateIpAddress")
        result["state"] = instance["State"]["Name"]
        result["vpc_id"] = vpc_id
        result["availability_zone"] = instance.get("Placement", {}).get("AvailabilityZone")
        add_specified_key_contract(
            result,
            requested_key_name=args.key_name,
            actual_key_name=instance.get("KeyName"),
        )
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
