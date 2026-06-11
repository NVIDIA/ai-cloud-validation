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

"""Test real network traffic flow using SSM to run ping commands.

Usage:
    python traffic_test.py --region us-west-2 --cidr 10.93.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_vpc": {"passed": true},
        "create_igw": {"passed": true},
        "create_iam": {"passed": true},
        "create_security_groups": {"passed": true},
        "launch_instances": {"passed": true},
        "instances_running": {"passed": true},
        "ssm_ready": {"passed": true},
        "traffic_allowed": {"passed": true, "latency_ms": 0.5},
        "traffic_blocked": {"passed": true},
        "internet_icmp": {"passed": true},
        "internet_http": {"passed": true}
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
from common.ec2 import (
    _parse_ping_latency,
    create_ssm_instance_profile,
    delete_ssm_instance_profile,
    run_ssm_command,
    wait_ssm_ready,
)
from common.errors import handle_aws_errors
from common.vpc import create_test_vpc


def create_igw(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Create and attach internet gateway."""
    result = {"passed": False}
    try:
        igw = ec2.create_internet_gateway()
        igw_id = igw["InternetGateway"]["InternetGatewayId"]

        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        ec2.create_tags(
            Resources=[igw_id],
            Tags=[
                {"Key": "Name", "Value": "isv-traffic-test-igw"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        result["passed"] = True
        result["igw_id"] = igw_id
        result["message"] = f"Created IGW {igw_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def ssm_instance_profile_result(iam: Any) -> dict[str, Any]:
    """Create an SSM instance profile and return test-result JSON."""
    try:
        role_name, profile_name = create_ssm_instance_profile(iam, "Temporary role for traffic testing")
        return {
            "passed": True,
            "role_name": role_name,
            "profile_name": profile_name,
            "message": f"Created IAM profile {profile_name}",
        }
    except ClientError as e:
        return {"passed": False, "error": str(e)}


def create_security_groups(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Create allow and deny security groups."""
    result = {"passed": False, "sg_allow": None, "sg_deny": None}

    try:
        # SG that allows ICMP
        sg_allow = ec2.create_security_group(
            GroupName=f"isv-allow-icmp-{uuid.uuid4().hex[:8]}",
            Description="Allow ICMP",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}]}],
        )
        sg_allow_id = sg_allow["GroupId"]

        ec2.authorize_security_group_ingress(
            GroupId=sg_allow_id,
            IpPermissions=[
                {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            ],
        )

        # SG that blocks ICMP (default behavior)
        sg_deny = ec2.create_security_group(
            GroupName=f"isv-deny-icmp-{uuid.uuid4().hex[:8]}",
            Description="Deny ICMP (default)",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}]}],
        )
        sg_deny_id = sg_deny["GroupId"]

        result["passed"] = True
        result["sg_allow"] = sg_allow_id
        result["sg_deny"] = sg_deny_id
        result["message"] = "Created allow and deny security groups"
    except ClientError as e:
        result["error"] = str(e)

    return result


def create_subnet_with_route(ec2: Any, vpc_id: str, cidr: str, igw_id: str) -> dict[str, Any]:
    """Create subnet with internet route."""
    result = {"passed": False}
    try:
        # Get AZ
        azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
        az = azs["AvailabilityZones"][0]["ZoneName"]

        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az)
        subnet_id = subnet["Subnet"]["SubnetId"]

        # Enable auto-assign public IP
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

        # Create route table with internet route
        rtb = ec2.create_route_table(VpcId=vpc_id)
        rtb_id = rtb["RouteTable"]["RouteTableId"]

        ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)

        result["passed"] = True
        result["subnet_id"] = subnet_id
        result["route_table_id"] = rtb_id
        result["message"] = f"Created subnet {subnet_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def get_amazon_linux_ami(ec2) -> str | None:
    """Get latest Amazon Linux 2 AMI."""
    response = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
    return images[0]["ImageId"] if images else None


def launch_instances(ec2: Any, subnet_id: str, sg_allow: str, sg_deny: str, profile_name: str) -> dict[str, Any]:
    """Launch test instances."""
    result = {"passed": False, "instances": []}

    ami = get_amazon_linux_ami(ec2)
    if not ami:
        result["error"] = "Could not find Amazon Linux AMI"
        return result

    try:
        # Source instance (for running SSM commands)
        source = ec2.run_instances(
            ImageId=ami,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_allow],
            IamInstanceProfile={"Name": profile_name},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": "isv-traffic-source"}, {"Key": "CreatedBy", "Value": "isvtest"}],
                }
            ],
        )
        source_id = source["Instances"][0]["InstanceId"]

        # Target with ICMP allowed
        target_allow = ec2.run_instances(
            ImageId=ami,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_allow],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": "isv-traffic-target-allow"},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        target_allow_id = target_allow["Instances"][0]["InstanceId"]

        # Target with ICMP blocked
        target_deny = ec2.run_instances(
            ImageId=ami,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_deny],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": "isv-traffic-target-deny"},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        target_deny_id = target_deny["Instances"][0]["InstanceId"]

        result["instances"] = [
            {"id": source_id, "role": "source"},
            {"id": target_allow_id, "role": "target_allow"},
            {"id": target_deny_id, "role": "target_deny"},
        ]
        result["passed"] = True
        result["message"] = "Launched 3 instances"
    except ClientError as e:
        result["error"] = str(e)

    return result


def wait_instances_running(ec2: Any, instance_ids: list[str]) -> dict[str, Any]:
    """Wait for instances to be running and get IPs."""
    result = {"passed": False, "instances": {}}

    try:
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=instance_ids)

        response = ec2.describe_instances(InstanceIds=instance_ids)
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                result["instances"][instance["InstanceId"]] = {
                    "state": instance["State"]["Name"],
                    "private_ip": instance.get("PrivateIpAddress"),
                    "public_ip": instance.get("PublicIpAddress"),
                }

        result["passed"] = True
        result["message"] = "All instances running"
    except ClientError as e:
        result["error"] = str(e)

    return result


def ssm_ready_result(ssm: Any, instance_id: str, timeout: int = 180) -> dict[str, Any]:
    """Wait for SSM readiness and return test-result JSON."""
    if wait_ssm_ready(ssm, instance_id, timeout):
        return {"passed": True, "message": "SSM agent online"}
    return {"passed": False, "error": f"SSM not ready after {timeout}s"}


def ping_result_via_ssm(ssm: Any, source_id: str, target_ip: str, expect_success: bool) -> dict[str, Any]:
    """Run ping through the shared SSM command helper and return test-result JSON."""
    ping_succeeded, output = run_ssm_command(ssm, source_id, f"ping -c 3 -W 2 {target_ip}")

    if expect_success:
        if ping_succeeded:
            latency = _parse_ping_latency(output)
            return {
                "passed": True,
                "latency_ms": latency,
                "message": f"Ping succeeded (latency: {latency}ms)",
            }
        return {"passed": False, "error": output or "Ping failed but expected to succeed"}

    if not ping_succeeded:
        return {"passed": True, "message": "Ping blocked as expected"}
    return {"passed": False, "error": "Ping succeeded but expected to be blocked"}


def test_internet_http(ssm: Any, source_id: str) -> dict[str, Any]:
    """Test HTTP/HTTPS to internet."""
    success, output = run_ssm_command(ssm, source_id, "curl -s --connect-timeout 5 https://checkip.amazonaws.com")
    if success:
        public_ip = output.strip()
        return {
            "passed": True,
            "public_ip": public_ip,
            "message": f"HTTPS succeeded (public IP: {public_ip})",
        }
    return {"passed": False, "error": output or "HTTPS request failed"}


def cleanup(
    ec2,
    iam,
    vpc_id: str,
    igw_id: str,
    subnet_id: str,
    rtb_id: str,
    sg_ids: list[str],
    instance_ids: list[str],
    role_name: str,
    profile_name: str,
) -> None:
    """Clean up all resources."""
    # Terminate instances
    if instance_ids:
        try:
            ec2.terminate_instances(InstanceIds=instance_ids)
            waiter = ec2.get_waiter("instance_terminated")
            waiter.wait(InstanceIds=instance_ids)
        except ClientError:
            pass

    # Delete security groups
    time.sleep(5)
    for sg_id in sg_ids:
        try:
            ec2.delete_security_group(GroupId=sg_id)
        except ClientError:
            pass

    # Delete subnet
    if subnet_id:
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
        except ClientError:
            pass

    # Delete route table
    if rtb_id:
        try:
            ec2.delete_route_table(RouteTableId=rtb_id)
        except ClientError:
            pass

    # Detach and delete IGW
    if igw_id:
        try:
            ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw_id)
        except ClientError:
            pass

    # Delete VPC
    if vpc_id:
        try:
            ec2.delete_vpc(VpcId=vpc_id)
        except ClientError:
            pass

    # Clean up IAM
    if profile_name and role_name:
        delete_ssm_instance_profile(iam, role_name, profile_name)


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test network traffic flow")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.93.0.0/16", help="CIDR for test VPC")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    suffix = str(uuid.uuid4())[:8]

    result = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    # Track resources for cleanup
    vpc_id = igw_id = subnet_id = rtb_id = None
    sg_ids = []
    instance_ids = []
    role_name = profile_name = None

    try:
        # Create VPC (with DNS enabled for SSM)
        vpc_result = create_test_vpc(ec2, args.cidr, f"isv-traffic-test-{suffix}", enable_dns=True)
        result["tests"]["create_vpc"] = vpc_result
        if not vpc_result["passed"]:
            raise RuntimeError("Failed to create VPC")
        vpc_id = vpc_result["vpc_id"]
        result["network_id"] = vpc_id

        # Create IGW
        igw_result = create_igw(ec2, vpc_id)
        result["tests"]["create_igw"] = igw_result
        if not igw_result["passed"]:
            raise RuntimeError("Failed to create IGW")
        igw_id = igw_result["igw_id"]

        # Create subnet
        subnet_cidr = args.cidr.replace(".0.0/16", ".1.0/24")
        subnet_result = create_subnet_with_route(ec2, vpc_id, subnet_cidr, igw_id)
        result["tests"]["network_setup"] = subnet_result
        if not subnet_result["passed"]:
            raise RuntimeError("Failed to create subnet")
        subnet_id = subnet_result["subnet_id"]
        rtb_id = subnet_result["route_table_id"]

        # Create IAM profile
        iam_result = ssm_instance_profile_result(iam)
        result["tests"]["create_iam"] = iam_result
        if not iam_result["passed"]:
            raise RuntimeError("Failed to create IAM")
        role_name = iam_result["role_name"]
        profile_name = iam_result["profile_name"]

        # Create security groups
        sg_result = create_security_groups(ec2, vpc_id)
        result["tests"]["create_security_groups"] = sg_result
        if not sg_result["passed"]:
            raise RuntimeError("Failed to create SGs")
        sg_allow = sg_result["sg_allow"]
        sg_deny = sg_result["sg_deny"]
        sg_ids = [sg_allow, sg_deny]

        # Launch instances
        launch_result = launch_instances(ec2, subnet_id, sg_allow, sg_deny, profile_name)
        result["tests"]["launch_instances"] = launch_result
        if not launch_result["passed"]:
            raise RuntimeError("Failed to launch instances")
        instance_ids = [i["id"] for i in launch_result["instances"]]

        # Wait for instances running
        running_result = wait_instances_running(ec2, instance_ids)
        result["tests"]["instances_running"] = running_result
        if not running_result["passed"]:
            raise RuntimeError("Instances not running")

        source_id = instance_ids[0]
        target_allow_ip = running_result["instances"][instance_ids[1]]["private_ip"]
        target_deny_ip = running_result["instances"][instance_ids[2]]["private_ip"]

        # Wait for SSM
        ssm_result = ssm_ready_result(ssm, source_id)
        result["tests"]["ssm_ready"] = ssm_result
        if not ssm_result["passed"]:
            raise RuntimeError("SSM not ready")

        # Test traffic allowed
        allowed_result = ping_result_via_ssm(ssm, source_id, target_allow_ip, expect_success=True)
        result["tests"]["traffic_allowed"] = allowed_result

        # Test traffic blocked
        blocked_result = ping_result_via_ssm(ssm, source_id, target_deny_ip, expect_success=False)
        result["tests"]["traffic_blocked"] = blocked_result

        # Test internet ICMP
        internet_icmp = ping_result_via_ssm(ssm, source_id, "8.8.8.8", expect_success=True)
        result["tests"]["internet_icmp"] = internet_icmp

        # Test internet HTTP
        internet_http = test_internet_http(ssm, source_id)
        result["tests"]["internet_http"] = internet_http

        # Check overall success
        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
        result["status"] = "failed"
    finally:
        cleanup(ec2, iam, vpc_id, igw_id, subnet_id, rtb_id, sg_ids, instance_ids, role_name, profile_name)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
