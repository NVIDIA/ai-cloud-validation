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

"""Launch GPU instance from imported AMI.

Creates an EC2 GPU instance from the imported AMI and waits for it to be ready.
Also sets up IAM role for SSM access to run validation commands.

Usage:
    python launch_instance.py --ami-id ami-xxx --instance-type g4dn.xlarge

Output (JSON):
    {
        "success": true,
        "platform": "image_registry",
        "instance_id": "i-xxx",
        "public_ip": "1.2.3.4",
        "private_ip": "10.0.0.1",
        "instance_type": "g4dn.xlarge",
        "region": "us-west-2",
        "key_name": "isv-iso-key-xxx",
        "security_group_id": "sg-xxx"
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import (
    create_key_pair,
    create_security_group,
    get_default_vpc_and_subnets,
)


def create_instance_profile(iam_client: Any, name: str) -> str | None:
    """Create IAM instance profile with SSM permissions."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }

    try:
        # Create role
        iam_client.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="ISV ISO validation instance role",
            Tags=[
                {"Key": "Name", "Value": name},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )
        print(f"Created IAM role: {name}", file=sys.stderr)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            print(f"Failed to create role: {e}", file=sys.stderr)
            return None

    # Attach SSM policy
    try:
        iam_client.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    except ClientError:
        pass  # Already attached

    # Create instance profile
    try:
        iam_client.create_instance_profile(InstanceProfileName=name)
        print(f"Created instance profile: {name}", file=sys.stderr)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            print(f"Failed to create instance profile: {e}", file=sys.stderr)
            return None

    # Add role to profile
    try:
        iam_client.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    except ClientError:
        pass  # Already added

    # Wait for propagation
    time.sleep(10)
    return name


def launch_instance(
    ec2_client: Any,
    ami_id: str,
    instance_type: str,
    key_name: str,
    security_group_id: str,
    subnet_id: str,
    instance_profile: str | None,
) -> dict | None:
    """Launch EC2 instance."""
    print(f"Launching {instance_type} instance with AMI {ami_id}...", file=sys.stderr)

    try:
        params = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "KeyName": key_name,
            "SecurityGroupIds": [security_group_id],
            "SubnetId": subnet_id,
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": f"isv-iso-validation-{uuid.uuid4().hex[:8]}"},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        }

        if instance_profile:
            params["IamInstanceProfile"] = {"Name": instance_profile}

        response = ec2_client.run_instances(**params)
        instance = response["Instances"][0]
        instance_id = instance["InstanceId"]
        print(f"Instance launched: {instance_id}", file=sys.stderr)
        return {"instance_id": instance_id}

    except ClientError as e:
        print(f"Failed to launch instance: {e}", file=sys.stderr)
        return None


def wait_for_instance(ec2_client: Any, instance_id: str, timeout: int = 300) -> dict | None:
    """Wait for instance to be running and get its details."""
    print(f"Waiting for instance {instance_id} to be running...", file=sys.stderr)

    waiter = ec2_client.get_waiter("instance_running")
    try:
        waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": timeout // 10})
    except Exception as e:
        print(f"Instance failed to start: {e}", file=sys.stderr)
        return None

    # Wait for status checks
    print("Waiting for instance status checks...", file=sys.stderr)
    try:
        status_waiter = ec2_client.get_waiter("instance_status_ok")
        status_waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
    except Exception as e:
        print(f"Warning: Status checks did not pass: {e}", file=sys.stderr)

    # Get instance details
    response = ec2_client.describe_instances(InstanceIds=[instance_id])
    instance = response["Reservations"][0]["Instances"][0]

    return {
        "instance_id": instance_id,
        "public_ip": instance.get("PublicIpAddress"),
        "private_ip": instance.get("PrivateIpAddress"),
        "state": instance["State"]["Name"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch GPU instance from AMI")
    parser.add_argument("--ami-id", required=True, help="AMI ID to launch from")
    parser.add_argument("--instance-type", default="g4dn.xlarge", help="Instance type")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--name-prefix", default="isv-iso", help="Prefix for resource names")
    args = parser.parse_args()

    # Initialize clients
    session = boto3.Session(region_name=args.region)
    ec2_client = session.client("ec2")
    iam_client = session.client("iam")

    unique_id = uuid.uuid4().hex[:8]
    key_name = f"{args.name_prefix}-key-{unique_id}"
    sg_name = f"{args.name_prefix}-sg-{unique_id}"
    profile_name = f"{args.name_prefix}-profile-{unique_id}"

    # Get VPC and subnet
    try:
        vpc_id, subnet_list = get_default_vpc_and_subnets(ec2_client, args.instance_type)
    except RuntimeError as e:
        result = {"success": False, "error": str(e)}
        print(json.dumps(result))
        return 1
    subnet_id = subnet_list[0]

    # Create key pair
    key_dir = Path.home() / ".ssh"
    key_dir.mkdir(exist_ok=True)
    try:
        key_path = create_key_pair(ec2_client, key_name, key_dir)
    except RuntimeError as e:
        result = {"success": False, "error": str(e)}
        print(json.dumps(result))
        return 1

    # Create security group
    sg_id = create_security_group(ec2_client, vpc_id, sg_name, description="ISV ISO validation security group")
    if not sg_id:
        result = {"success": False, "error": "Failed to create security group"}
        print(json.dumps(result))
        return 1

    # Create instance profile (for SSM)
    instance_profile = create_instance_profile(iam_client, profile_name)

    # Launch instance
    launch_result = launch_instance(
        ec2_client,
        args.ami_id,
        args.instance_type,
        key_name,
        sg_id,
        subnet_id,
        instance_profile,
    )
    if not launch_result:
        result = {"success": False, "error": "Failed to launch instance"}
        print(json.dumps(result))
        return 1

    # Wait for instance
    instance_info = wait_for_instance(ec2_client, launch_result["instance_id"])
    if not instance_info:
        result = {"success": False, "error": "Instance failed to start"}
        print(json.dumps(result))
        return 1

    result = {
        "success": True,
        "platform": "image_registry",
        # Generic instance fields (provider-agnostic)
        "instance_id": instance_info["instance_id"],
        "public_ip": instance_info.get("public_ip"),
        "private_ip": instance_info.get("private_ip"),
        "state": instance_info.get("state"),
        "instance_type": args.instance_type,
        "region": args.region,
        "key_name": key_name,
        "key_path": str(key_path),
        "ssh_user": "ubuntu",  # Default SSH user for the image
        # Generic image reference
        "image_id": args.ami_id,
        # AWS-specific fields (for reference)
        "ami_id": args.ami_id,
        "security_group_id": sg_id,
        "security_group_name": sg_name,
        "instance_profile": instance_profile,
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
