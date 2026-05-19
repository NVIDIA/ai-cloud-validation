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

"""Test that private IP addresses remain stable across instance stop/start.

Creates an instance, records its private IP, stops and restarts it,
then verifies the private IP is unchanged.

Usage:
    python stable_ip_test.py --region us-west-2 --cidr 10.91.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_instance": {"passed": true, "instance_id": "i-xxx"},
        "record_ip": {"passed": true, "private_ip": "10.91.1.x"},
        "stop_instance": {"passed": true},
        "start_instance": {"passed": true},
        "ip_unchanged": {"passed": true, "ip_before": "10.91.1.x", "ip_after": "10.91.1.x"}
    }
}
"""

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError, WaiterError
from common.ec2 import get_amazon_linux_ami
from common.errors import delete_with_retry, handle_aws_errors
from common.vpc import create_test_vpc, delete_vpc


def create_instance(ec2: Any, subnet_id: str, sg_id: str, name: str) -> dict[str, Any]:
    """Launch an instance and wait for it to run."""
    result: dict[str, Any] = {"passed": False}

    ami = get_amazon_linux_ami(ec2)
    if not ami:
        result["error"] = "Could not find Amazon Linux AMI"
        return result

    try:
        response = ec2.run_instances(
            ImageId=ami,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": name},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        instance_id = response["Instances"][0]["InstanceId"]

        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        result["passed"] = True
        result["instance_id"] = instance_id
        result["message"] = f"Launched instance {instance_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def record_ip(ec2: Any, instance_id: str) -> dict[str, Any]:
    """Record the current private IP of an instance."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        instance = response["Reservations"][0]["Instances"][0]
        private_ip = instance.get("PrivateIpAddress")

        if private_ip:
            result["passed"] = True
            result["private_ip"] = private_ip
            result["message"] = f"Instance {instance_id} has private IP {private_ip}"
        else:
            result["error"] = "No private IP assigned"
    except ClientError as e:
        result["error"] = str(e)

    return result


def stop_instance(ec2: Any, instance_id: str) -> dict[str, Any]:
    """Stop an instance and wait for it to stop."""
    result: dict[str, Any] = {"passed": False}

    try:
        ec2.stop_instances(InstanceIds=[instance_id])
        waiter = ec2.get_waiter("instance_stopped")
        waiter.wait(InstanceIds=[instance_id])

        result["passed"] = True
        result["message"] = f"Instance {instance_id} stopped"
    except ClientError as e:
        result["error"] = str(e)

    return result


def start_instance(ec2: Any, instance_id: str) -> dict[str, Any]:
    """Start an instance and wait for it to run."""
    result: dict[str, Any] = {"passed": False}

    try:
        ec2.start_instances(InstanceIds=[instance_id])
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        # Small delay for network interface stabilization
        time.sleep(5)

        result["passed"] = True
        result["message"] = f"Instance {instance_id} started"
    except ClientError as e:
        result["error"] = str(e)

    return result


def check_ip_unchanged(ec2: Any, instance_id: str, original_ip: str) -> dict[str, Any]:
    """Verify the private IP is the same as before stop/start."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        instance = response["Reservations"][0]["Instances"][0]
        current_ip = instance.get("PrivateIpAddress")

        result["ip_before"] = original_ip
        result["ip_after"] = current_ip

        if current_ip == original_ip:
            result["passed"] = True
            result["message"] = f"Private IP stable: {current_ip} unchanged after stop/start"
        else:
            result["error"] = f"IP changed from {original_ip} to {current_ip}"
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test stable private IP")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.91.0.0/16", help="CIDR for test VPC")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    suffix = str(uuid.uuid4())[:8]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    vpc_id = None
    subnet_id = None
    sg_id = None
    instance_id = None

    try:
        # Setup: Create VPC with subnet
        vpc_result = create_test_vpc(ec2, args.cidr, f"isv-stable-ip-{suffix}")
        if not vpc_result["passed"]:
            result["tests"]["setup_vpc"] = vpc_result
            raise RuntimeError("Failed to create VPC")
        vpc_id = vpc_result["vpc_id"]

        azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
        az = azs["AvailabilityZones"][0]["ZoneName"]

        subnet = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=args.cidr.replace(".0.0/16", ".1.0/24"),
            AvailabilityZone=az,
        )
        subnet_id = subnet["Subnet"]["SubnetId"]

        sg = ec2.create_security_group(
            GroupName=f"isv-stable-ip-sg-{suffix}",
            Description="Stable IP test SG",
            VpcId=vpc_id,
        )
        sg_id = sg["GroupId"]

        # Test 1: Create instance
        create_result = create_instance(ec2, subnet_id, sg_id, f"isv-stable-ip-{suffix}")
        result["tests"]["create_instance"] = create_result
        if not create_result["passed"]:
            raise RuntimeError("Failed to create instance")
        instance_id = create_result["instance_id"]

        # Test 2: Record initial IP
        ip_result = record_ip(ec2, instance_id)
        result["tests"]["record_ip"] = ip_result
        if not ip_result["passed"]:
            raise RuntimeError("Failed to record IP")
        original_ip = ip_result["private_ip"]

        # Test 3: Stop instance
        stop_result = stop_instance(ec2, instance_id)
        result["tests"]["stop_instance"] = stop_result
        if not stop_result["passed"]:
            raise RuntimeError("Failed to stop instance")

        # Test 4: Start instance
        start_result = start_instance(ec2, instance_id)
        result["tests"]["start_instance"] = start_result
        if not start_result["passed"]:
            raise RuntimeError("Failed to start instance")

        # Test 5: Verify IP unchanged
        ip_check = check_ip_unchanged(ec2, instance_id, original_ip)
        result["tests"]["ip_unchanged"] = ip_check

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        if instance_id:
            # terminate_instances + waiter wait is a single best-effort unit;
            # retry covers the terminate API call, then wait for settle.
            if delete_with_retry(
                ec2.terminate_instances,
                InstanceIds=[instance_id],
                resource_desc=f"instance {instance_id}",
            ):
                try:
                    ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
                except (ClientError, WaiterError):
                    # WaiterError fires on timeout or terminal-failure state; if
                    # we let it escape, the finally block exits early and leaks
                    # the SG, subnet, and VPC that still need cleanup below.
                    pass
            time.sleep(5)
        if sg_id:
            delete_with_retry(
                ec2.delete_security_group,
                GroupId=sg_id,
                resource_desc=f"security group {sg_id}",
            )
        if subnet_id:
            delete_with_retry(
                ec2.delete_subnet,
                SubnetId=subnet_id,
                resource_desc=f"subnet {subnet_id}",
            )
        if vpc_id:
            delete_vpc(ec2, vpc_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
