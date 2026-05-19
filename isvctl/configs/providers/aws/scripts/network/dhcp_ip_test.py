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

"""DHCP/IP management test - AWS reference implementation.

Creates an EC2 key pair, launches a t3.micro instance in the specified subnet,
and outputs SSH connection details for the DhcpIpManagementCheck validation.

Usage:
    python dhcp_ip_test.py --vpc-id vpc-xxx --subnet-id subnet-xxx \\
        --sg-id sg-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "dhcp_ip",
    "public_ip": "3.1.2.3",
    "private_ip": "10.0.1.5",
    "key_file": "/tmp/isv-dhcp-test-key.pem",
    "key_name": "isv-dhcp-test-key",
    "ssh_user": "ubuntu",
    "instance_id": "i-abc123"
}
"""

import argparse
import json
import os
import socket
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import create_key_pair
from common.errors import classify_aws_error, handle_aws_errors

# Ubuntu 22.04 LTS AMI (us-west-2) - update per region as needed
DEFAULT_AMI = "ami-0735c191cf914754d"
INSTANCE_TYPE = "t3.micro"
KEY_NAME_PREFIX = "isv-dhcp-test-key"


def find_ubuntu_ami(ec2: Any) -> str:
    """Find the latest Ubuntu 22.04 LTS AMI for the region."""
    try:
        response = ec2.describe_images(
            Filters=[
                {"Name": "name", "Values": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]},
                {"Name": "state", "Values": ["available"]},
                {"Name": "architecture", "Values": ["x86_64"]},
            ],
            Owners=["099720109477"],  # Canonical
        )
        images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
        if images:
            return images[0]["ImageId"]
    except ClientError:
        pass
    return DEFAULT_AMI


def launch_instance(
    ec2: Any,
    subnet_id: str,
    sg_id: str,
    key_name: str,
    ami_id: str,
) -> dict[str, Any]:
    """Launch an EC2 instance and wait for it to be running."""
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=INSTANCE_TYPE,
        KeyName=key_name,
        MinCount=1,
        MaxCount=1,
        NetworkInterfaces=[
            {
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "Groups": [sg_id],
                "AssociatePublicIpAddress": True,
            },
        ],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": "isv-dhcp-test"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            },
        ],
    )

    instance_id = response["Instances"][0]["InstanceId"]

    # Wait for running state
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    # Describe to get IPs
    desc = ec2.describe_instances(InstanceIds=[instance_id])
    instance = desc["Reservations"][0]["Instances"][0]

    return {
        "instance_id": instance_id,
        "public_ip": instance.get("PublicIpAddress"),
        "private_ip": instance.get("PrivateIpAddress"),
    }


def wait_for_ssh(public_ip: str, timeout: int = 120) -> bool:
    """Wait for SSH port to become reachable (simple TCP check)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((public_ip, 22), timeout=5):
                return True
        except OSError:
            time.sleep(5)
    return False


@handle_aws_errors
def main() -> int:
    """Create key pair, launch instance, output SSH details for DHCP validation."""
    parser = argparse.ArgumentParser(description="DHCP/IP management test (AWS)")
    parser.add_argument("--vpc-id", required=True, help="VPC ID")
    parser.add_argument("--subnet-id", required=True, help="Subnet to launch instance in")
    parser.add_argument("--sg-id", required=True, help="Security group ID")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--ami-id", default=None, help="AMI ID (auto-detects Ubuntu if not set)")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    key_name = f"{KEY_NAME_PREFIX}-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "dhcp_ip",
        "public_ip": None,
        "private_ip": None,
        "key_file": None,
        "key_name": key_name,
        "ssh_user": args.ssh_user,
        "instance_id": None,
    }

    try:
        # Create key pair with unique name
        key_file = create_key_pair(ec2, key_name)
        result["key_file"] = key_file

        # Find AMI
        ami_id = args.ami_id or find_ubuntu_ami(ec2)

        # Launch instance
        instance_info = launch_instance(ec2, args.subnet_id, args.sg_id, key_name, ami_id)
        result["instance_id"] = instance_info["instance_id"]
        result["public_ip"] = instance_info["public_ip"]
        result["private_ip"] = instance_info["private_ip"]

        if not result["public_ip"]:
            result["error"] = "Instance launched but no public IP assigned"
            print(json.dumps(result, indent=2))
            return 1

        # Wait for SSH to become available
        if not wait_for_ssh(result["public_ip"]):
            result["error"] = f"SSH not reachable on {result['public_ip']} after 120s"
            print(json.dumps(result, indent=2))
            return 1

        result["success"] = True

    except ClientError as e:
        result["error_type"], result["error"] = classify_aws_error(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
