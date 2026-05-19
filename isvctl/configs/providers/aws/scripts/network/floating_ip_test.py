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

"""Test atomically switching a floating IP between instances.

Allocates an Elastic IP, associates it with instance A, then reassociates
to instance B, and verifies the switch completes within the allowed threshold.

Usage:
    python floating_ip_test.py --region us-west-2 --cidr 10.92.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "allocate_eip": {"passed": true, "allocation_id": "eipalloc-xxx", "public_ip": "x.x.x.x"},
        "associate_to_a": {"passed": true},
        "verify_on_a": {"passed": true},
        "reassociate_to_b": {"passed": true, "switch_seconds": 2.3},
        "verify_on_b": {"passed": true},
        "verify_not_on_a": {"passed": true}
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
from botocore.exceptions import ClientError
from common.ec2 import get_amazon_linux_ami
from common.errors import handle_aws_errors
from common.vpc import create_test_vpc, delete_vpc

MAX_SWITCH_SECONDS = 10


def allocate_eip(ec2: Any) -> dict[str, Any]:
    """Allocate an Elastic IP address."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.allocate_address(Domain="vpc")
        result["passed"] = True
        result["allocation_id"] = response["AllocationId"]
        result["public_ip"] = response["PublicIp"]
        result["message"] = f"Allocated EIP {response['PublicIp']}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def associate_eip(ec2: Any, allocation_id: str, instance_id: str) -> dict[str, Any]:
    """Associate an EIP with an instance."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.associate_address(
            AllocationId=allocation_id,
            InstanceId=instance_id,
            AllowReassociation=True,
        )
        result["passed"] = True
        result["association_id"] = response["AssociationId"]
        result["message"] = f"Associated EIP with {instance_id}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def verify_eip_on_instance(ec2: Any, instance_id: str, expected_ip: str) -> dict[str, Any]:
    """Verify the EIP is associated with the expected instance."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        instance = response["Reservations"][0]["Instances"][0]
        public_ip = instance.get("PublicIpAddress")

        if public_ip == expected_ip:
            result["passed"] = True
            result["public_ip"] = public_ip
            result["message"] = f"EIP {expected_ip} confirmed on {instance_id}"
        else:
            result["error"] = f"Expected {expected_ip}, got {public_ip}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def verify_eip_not_on_instance(ec2: Any, instance_id: str, eip: str) -> dict[str, Any]:
    """Verify the EIP is no longer associated with this instance."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        instance = response["Reservations"][0]["Instances"][0]
        public_ip = instance.get("PublicIpAddress")

        if public_ip != eip:
            result["passed"] = True
            result["message"] = f"EIP {eip} correctly removed from {instance_id}"
        else:
            result["error"] = f"EIP {eip} still on {instance_id}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def reassociate_eip_timed(
    ec2: Any, allocation_id: str, target_instance_id: str, max_seconds: int = MAX_SWITCH_SECONDS
) -> dict[str, Any]:
    """Reassociate an EIP to a different instance and measure the switch time."""
    result: dict[str, Any] = {"passed": False}

    try:
        start = time.monotonic()
        ec2.associate_address(
            AllocationId=allocation_id,
            InstanceId=target_instance_id,
            AllowReassociation=True,
        )
        elapsed = time.monotonic() - start

        result["switch_seconds"] = round(elapsed, 2)

        if elapsed <= max_seconds:
            result["passed"] = True
            result["message"] = f"EIP switched in {elapsed:.2f}s (limit: {max_seconds}s)"
        else:
            result["error"] = f"Switch took {elapsed:.2f}s, exceeds {max_seconds}s limit"
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test floating IP switch")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.92.0.0/16", help="CIDR for test VPC")
    parser.add_argument(
        "--max-switch-seconds",
        type=int,
        default=MAX_SWITCH_SECONDS,
        help="Maximum allowed switch time in seconds",
    )
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
    instance_ids: list[str] = []
    allocation_id = None
    igw_id = None
    rtb_id = None
    rtb_assoc_id = None

    try:
        # Setup: VPC, subnet, IGW, security group, two instances
        vpc_result = create_test_vpc(ec2, args.cidr, f"isv-floating-ip-{suffix}")
        if not vpc_result["passed"]:
            result["tests"]["setup_vpc"] = vpc_result
            raise RuntimeError("Failed to create VPC")
        vpc_id = vpc_result["vpc_id"]

        # IGW needed for EIP
        igw = ec2.create_internet_gateway()
        igw_id = igw["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
        az = azs["AvailabilityZones"][0]["ZoneName"]

        subnet = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=args.cidr.replace(".0.0/16", ".1.0/24"),
            AvailabilityZone=az,
        )
        subnet_id = subnet["Subnet"]["SubnetId"]
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

        # Route table with IGW
        rtb = ec2.create_route_table(VpcId=vpc_id)
        rtb_id = rtb["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        assoc = ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)
        rtb_assoc_id = assoc["AssociationId"]

        sg = ec2.create_security_group(
            GroupName=f"isv-floating-ip-sg-{suffix}",
            Description="Floating IP test SG",
            VpcId=vpc_id,
        )
        sg_id = sg["GroupId"]

        ami = get_amazon_linux_ami(ec2)
        if not ami:
            raise RuntimeError("Could not find AMI")

        for label in ["a", "b"]:
            resp = ec2.run_instances(
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
                            {"Key": "Name", "Value": f"isv-floating-ip-{label}-{suffix}"},
                            {"Key": "CreatedBy", "Value": "isvtest"},
                        ],
                    }
                ],
            )
            instance_ids.append(resp["Instances"][0]["InstanceId"])

        ec2.get_waiter("instance_running").wait(InstanceIds=instance_ids)

        instance_a, instance_b = instance_ids

        # Test 1: Allocate EIP
        alloc_result = allocate_eip(ec2)
        result["tests"]["allocate_eip"] = alloc_result
        if not alloc_result["passed"]:
            raise RuntimeError("Failed to allocate EIP")
        allocation_id = alloc_result["allocation_id"]
        eip = alloc_result["public_ip"]

        # Test 2: Associate to instance A
        assoc_result = associate_eip(ec2, allocation_id, instance_a)
        result["tests"]["associate_to_a"] = assoc_result
        if not assoc_result["passed"]:
            raise RuntimeError("Failed to associate EIP")

        time.sleep(3)

        # Test 3: Verify on A
        verify_a = verify_eip_on_instance(ec2, instance_a, eip)
        result["tests"]["verify_on_a"] = verify_a

        # Test 4: Reassociate to B (timed)
        reassoc = reassociate_eip_timed(ec2, allocation_id, instance_b, args.max_switch_seconds)
        result["tests"]["reassociate_to_b"] = reassoc

        time.sleep(3)

        # Test 5: Verify on B
        verify_b = verify_eip_on_instance(ec2, instance_b, eip)
        result["tests"]["verify_on_b"] = verify_b

        # Test 6: Verify no longer on A
        verify_gone = verify_eip_not_on_instance(ec2, instance_a, eip)
        result["tests"]["verify_not_on_a"] = verify_gone

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        # Cleanup
        if instance_ids:
            try:
                ec2.terminate_instances(InstanceIds=instance_ids)
                ec2.get_waiter("instance_terminated").wait(InstanceIds=instance_ids)
            except ClientError:
                pass
            time.sleep(5)

        if allocation_id:
            try:
                # Disassociate first
                addrs = ec2.describe_addresses(AllocationIds=[allocation_id])
                for addr in addrs.get("Addresses", []):
                    if addr.get("AssociationId"):
                        ec2.disassociate_address(AssociationId=addr["AssociationId"])
                ec2.release_address(AllocationId=allocation_id)
            except ClientError:
                pass

        if sg_id:
            try:
                ec2.delete_security_group(GroupId=sg_id)
            except ClientError:
                pass
        if rtb_assoc_id:
            try:
                ec2.disassociate_route_table(AssociationId=rtb_assoc_id)
            except ClientError:
                pass
        if rtb_id:
            try:
                ec2.delete_route_table(RouteTableId=rtb_id)
            except ClientError:
                pass
        if subnet_id:
            try:
                ec2.delete_subnet(SubnetId=subnet_id)
            except ClientError:
                pass
        if igw_id:
            try:
                ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
                ec2.delete_internet_gateway(InternetGatewayId=igw_id)
            except ClientError:
                pass
        if vpc_id:
            delete_vpc(ec2, vpc_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
