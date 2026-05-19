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

"""Test network connectivity between instances in VPC.

Platform-specific script that uses boto3 to launch instances and SSM to test.
Outputs JSON for validation assertions.

Usage:
    python test_connectivity.py --vpc-id vpc-xxx --subnet-ids subnet-a,subnet-b --sg-id sg-xxx

Output JSON:
{
    "success": true,
    "tests": {
        "instance_to_instance": {"passed": true, "latency_ms": 0.5},
        "instance_to_internet": {"passed": true}
    },
    "instances": [
        {"instance_id": "i-xxx", "private_ip": "10.0.1.5"}
    ]
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
from common.errors import handle_aws_errors

SSM_ROLE_TRUST_POLICY = """{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }
    ]
}"""


def create_ssm_instance_profile(iam: Any) -> tuple[str, str]:
    """Create IAM role and instance profile for SSM."""
    suffix = str(uuid.uuid4())[:8]
    role_name = f"isv-ssm-role-{suffix}"
    profile_name = f"isv-ssm-profile-{suffix}"

    # Create role
    iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=SSM_ROLE_TRUST_POLICY,
        Description="Temporary role for SSM connectivity testing",
        Tags=[
            {"Key": "Name", "Value": role_name},
            {"Key": "CreatedBy", "Value": "isvtest"},
        ],
    )

    # Attach SSM managed policy
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    )

    # Create instance profile
    iam.create_instance_profile(InstanceProfileName=profile_name)
    iam.add_role_to_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)

    # Wait for profile to be ready
    time.sleep(10)

    return role_name, profile_name


def delete_ssm_instance_profile(iam: Any, role_name: str, profile_name: str) -> None:
    """Delete IAM role and instance profile."""
    try:
        iam.remove_role_from_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)
    except ClientError:
        pass
    try:
        iam.delete_instance_profile(InstanceProfileName=profile_name)
    except ClientError:
        pass
    try:
        iam.detach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )
    except ClientError:
        pass
    try:
        iam.delete_role(RoleName=role_name)
    except ClientError:
        pass


def get_amazon_linux_ami(ec2: Any) -> str | None:
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


def launch_instances(
    ec2: Any, subnet_ids: list[str], sg_id: str, instance_profile: str | None = None
) -> list[dict[str, Any]]:
    """Launch test instances."""
    ami = get_amazon_linux_ami(ec2)
    if not ami:
        raise RuntimeError("Could not find Amazon Linux AMI")

    instances = []
    for i, subnet_id in enumerate(subnet_ids[:2]):
        params: dict[str, Any] = {
            "ImageId": ami,
            "InstanceType": "t3.micro",
            "MinCount": 1,
            "MaxCount": 1,
            "SubnetId": subnet_id,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": f"isv-connectivity-test-{i}"},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        }
        if sg_id:
            params["SecurityGroupIds"] = [sg_id]
        if instance_profile:
            params["IamInstanceProfile"] = {"Name": instance_profile}

        response = ec2.run_instances(**params)
        instances.append(
            {
                "instance_id": response["Instances"][0]["InstanceId"],
                "subnet_id": subnet_id,
            }
        )

    # Wait for running
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[i["instance_id"] for i in instances])

    # Get IPs and VPC info
    response = ec2.describe_instances(InstanceIds=[i["instance_id"] for i in instances])
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            for inst in instances:
                if inst["instance_id"] == instance["InstanceId"]:
                    inst["private_ip"] = instance.get("PrivateIpAddress")
                    inst["public_ip"] = instance.get("PublicIpAddress")
                    inst["vpc_id"] = instance.get("VpcId")

    return instances


def test_ping_ssm(ssm: Any, instance_id: str, target: str) -> dict[str, Any]:
    """Test ping using SSM."""
    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [f"ping -c 3 -W 2 {target}"]},
        )
        command_id = response["Command"]["CommandId"]

        for _ in range(30):
            time.sleep(2)
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            if result["Status"] in ["Success", "Failed", "TimedOut"]:
                if result["Status"] == "Success":
                    # Parse latency from ping output
                    output = result.get("StandardOutputContent", "")
                    latency = None
                    for line in output.split("\n"):
                        if "avg" in line:
                            parts = line.split("=")[-1].split("/")
                            if len(parts) >= 2:
                                latency = float(parts[1])
                    return {"passed": True, "latency_ms": latency}
                return {"passed": False, "error": result.get("StandardErrorContent", "Failed")}

        return {"passed": False, "error": "Timeout"}
    except ClientError as e:
        return {"passed": False, "error": str(e)}


def terminate_instances(ec2: Any, instance_ids: list[str]) -> None:
    """Terminate instances."""
    if instance_ids:
        ec2.terminate_instances(InstanceIds=instance_ids)


def validate_vpc_resources(ec2: Any, vpc_id: str, subnet_ids: list[str], sg_id: str) -> dict[str, Any]:
    """Validate that subnets and security group belong to the specified VPC.

    Args:
        ec2: boto3 EC2 client
        vpc_id: Expected VPC ID
        subnet_ids: List of subnet IDs to validate
        sg_id: Security group ID to validate

    Returns:
        dict with validation results and any errors
    """
    validation = {"valid": True, "errors": [], "validated_subnets": [], "validated_sg": None}

    # Validate subnets belong to VPC
    try:
        subnets = ec2.describe_subnets(SubnetIds=subnet_ids)
        for subnet in subnets["Subnets"]:
            subnet_vpc = subnet["VpcId"]
            subnet_id = subnet["SubnetId"]
            if subnet_vpc != vpc_id:
                validation["valid"] = False
                validation["errors"].append(f"Subnet {subnet_id} belongs to VPC {subnet_vpc}, not {vpc_id}")
            else:
                validation["validated_subnets"].append(subnet_id)
    except ClientError as e:
        validation["valid"] = False
        validation["errors"].append(f"Failed to validate subnets: {e}")

    # Validate security group belongs to VPC
    if sg_id:
        try:
            sgs = ec2.describe_security_groups(GroupIds=[sg_id])
            if sgs["SecurityGroups"]:
                sg_vpc = sgs["SecurityGroups"][0]["VpcId"]
                if sg_vpc != vpc_id:
                    validation["valid"] = False
                    validation["errors"].append(f"Security group {sg_id} belongs to VPC {sg_vpc}, not {vpc_id}")
                else:
                    validation["validated_sg"] = sg_id
        except ClientError as e:
            validation["valid"] = False
            validation["errors"].append(f"Failed to validate security group: {e}")

    return validation


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test VPC connectivity")
    parser.add_argument("--vpc-id", required=True, help="VPC ID")
    parser.add_argument("--subnet-ids", required=True, help="Comma-separated subnet IDs")
    parser.add_argument("--sg-id", required=True, help="Security group ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()

    subnet_ids = args.subnet_ids.split(",")

    ec2 = boto3.client("ec2", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "vpc_id": args.vpc_id,
        "tests": {},
        "instances": [],
    }

    instance_ids = []
    role_name = None
    profile_name = None

    try:
        # Validate that subnets and security group belong to the specified VPC
        vpc_validation = validate_vpc_resources(ec2, args.vpc_id, subnet_ids, args.sg_id)
        result["vpc_validation"] = vpc_validation

        if not vpc_validation["valid"]:
            result["error"] = f"VPC validation failed: {'; '.join(vpc_validation['errors'])}"
            result["status"] = "failed"
            print(json.dumps(result, indent=2))
            return 1

        # Create IAM role and instance profile for SSM
        role_name, profile_name = create_ssm_instance_profile(iam)
        result["iam_profile"] = profile_name

        instances = launch_instances(ec2, subnet_ids, args.sg_id, profile_name)
        result["instances"] = instances
        instance_ids = [i["instance_id"] for i in instances]

        # Verify launched instances are in the correct VPC
        response = ec2.describe_instances(InstanceIds=instance_ids)
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                instance_vpc = instance.get("VpcId")
                if instance_vpc != args.vpc_id:
                    raise RuntimeError(
                        f"Instance {instance['InstanceId']} launched in VPC {instance_vpc}, expected {args.vpc_id}"
                    )

        # Wait for SSM agent to register (needs longer with IAM profile)
        time.sleep(90)

        # Test instance-to-instance
        if len(instances) >= 2:
            test_result = test_ping_ssm(ssm, instances[0]["instance_id"], instances[1]["private_ip"])
            result["tests"]["instance_to_instance"] = test_result

        # Test internet
        test_result = test_ping_ssm(ssm, instances[0]["instance_id"], "8.8.8.8")
        result["tests"]["instance_to_internet"] = test_result

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
        result["status"] = "failed"
    finally:
        if not args.skip_cleanup:
            if instance_ids:
                terminate_instances(ec2, instance_ids)
                # Wait for instances to terminate before deleting IAM resources
                time.sleep(30)
            if role_name and profile_name:
                delete_ssm_instance_profile(iam, role_name, profile_name)
            result["cleanup"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
