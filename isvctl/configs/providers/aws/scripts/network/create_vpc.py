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

"""Create AWS VPC with subnets for testing.

Usage:
    python create_vpc.py --name test-vpc --region us-west-2 --cidr 10.0.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "network_id": "vpc-xxx",
    "cidr": "10.0.0.0/16",
    "subnets": [
        {"subnet_id": "subnet-xxx", "cidr": "10.0.1.0/24", "az": "us-west-2a",
         "auto_assign_public_ip": true, "available_ips": 251},
        {"subnet_id": "subnet-yyy", "cidr": "10.0.2.0/24", "az": "us-west-2b",
         "auto_assign_public_ip": true, "available_ips": 251}
    ],
    "internet_gateway_id": "igw-xxx",
    "route_table_id": "rtb-xxx",
    "security_group_id": "sg-xxx",
    "dhcp_options": {
        "dhcp_options_id": "dopt-xxx",
        "domain_name": "ec2.internal",
        "domain_name_servers": ["AmazonProvidedDNS"],
        "ntp_servers": []
    }
}
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import classify_aws_error, handle_aws_errors


def create_vpc(ec2: Any, name: str, cidr: str) -> dict[str, Any]:
    """Create VPC with all required components."""
    result = {
        "success": False,
        "platform": "network",
        "network_id": None,  # Generic field for validation
        "cidr": cidr,
        "subnets": [],
        "internet_gateway_id": None,
        "route_table_id": None,
        "security_group_id": None,
        "dhcp_options": None,
    }

    tag_suffix = str(uuid.uuid4())[:8]

    try:
        # Create VPC
        vpc = ec2.create_vpc(CidrBlock=cidr)
        vpc_id = vpc["Vpc"]["VpcId"]
        result["network_id"] = vpc_id  # Generic field for validation

        # Wait for VPC to be available
        waiter = ec2.get_waiter("vpc_available")
        waiter.wait(VpcIds=[vpc_id])

        # Tag VPC
        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {"Key": "Name", "Value": f"{name}-{tag_suffix}"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        # Enable DNS hostnames
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        # Create Internet Gateway
        igw = ec2.create_internet_gateway()
        igw_id = igw["InternetGateway"]["InternetGatewayId"]
        result["internet_gateway_id"] = igw_id

        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        ec2.create_tags(
            Resources=[igw_id],
            Tags=[
                {"Key": "Name", "Value": f"{name}-igw-{tag_suffix}"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        # Get availability zones
        azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
        az_names = [az["ZoneName"] for az in azs["AvailabilityZones"][:2]]

        # Create subnets
        subnet_cidrs = ["10.0.1.0/24", "10.0.2.0/24"]
        for i, (az, subnet_cidr) in enumerate(zip(az_names, subnet_cidrs)):
            subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=az)
            subnet_id = subnet["Subnet"]["SubnetId"]

            ec2.create_tags(
                Resources=[subnet_id],
                Tags=[
                    {"Key": "Name", "Value": f"{name}-subnet-{i}-{tag_suffix}"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            )

            # Enable auto-assign public IP
            ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

            # Describe subnet to get available IP count
            desc = ec2.describe_subnets(SubnetIds=[subnet_id])
            available_ips = desc["Subnets"][0].get("AvailableIpAddressCount", 0)

            result["subnets"].append(
                {
                    "subnet_id": subnet_id,
                    "cidr": subnet_cidr,
                    "az": az,
                    "auto_assign_public_ip": True,
                    "available_ips": available_ips,
                }
            )

        # Create route table
        rtb = ec2.create_route_table(VpcId=vpc_id)
        rtb_id = rtb["RouteTable"]["RouteTableId"]
        result["route_table_id"] = rtb_id

        ec2.create_tags(
            Resources=[rtb_id],
            Tags=[
                {"Key": "Name", "Value": f"{name}-rtb-{tag_suffix}"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        # Add route to internet gateway
        ec2.create_route(
            RouteTableId=rtb_id,
            DestinationCidrBlock="0.0.0.0/0",
            GatewayId=igw_id,
        )

        # Associate route table with subnets
        for subnet in result["subnets"]:
            ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet["subnet_id"])

        # Create security group
        sg = ec2.create_security_group(
            GroupName=f"{name}-sg-{tag_suffix}",
            Description="Test security group",
            VpcId=vpc_id,
        )
        sg_id = sg["GroupId"]
        result["security_group_id"] = sg_id

        # Allow SSH and ICMP
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {
                    "IpProtocol": "icmp",
                    "FromPort": -1,
                    "ToPort": -1,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
            ],
        )

        # Collect DHCP options for VPC
        vpc_desc = ec2.describe_vpcs(VpcIds=[vpc_id])
        dhcp_options_id = vpc_desc["Vpcs"][0].get("DhcpOptionsId")
        if dhcp_options_id and dhcp_options_id != "default":
            dhcp_resp = ec2.describe_dhcp_options(DhcpOptionsIds=[dhcp_options_id])
            dhcp_cfg = dhcp_resp["DhcpOptions"][0].get("DhcpConfigurations", [])
            dhcp_info: dict[str, Any] = {
                "dhcp_options_id": dhcp_options_id,
                "domain_name": None,
                "domain_name_servers": [],
                "ntp_servers": [],
            }
            for cfg in dhcp_cfg:
                vals = [v["Value"] for v in cfg.get("Values", [])]
                if cfg["Key"] == "domain-name":
                    dhcp_info["domain_name"] = vals[0] if vals else None
                elif cfg["Key"] == "domain-name-servers":
                    dhcp_info["domain_name_servers"] = vals
                elif cfg["Key"] == "ntp-servers":
                    dhcp_info["ntp_servers"] = vals
            result["dhcp_options"] = dhcp_info
        else:
            result["dhcp_options"] = {
                "dhcp_options_id": dhcp_options_id or "default",
                "domain_name": "ec2.internal",
                "domain_name_servers": ["AmazonProvidedDNS"],
                "ntp_servers": [],
            }

        result["success"] = True

    except ClientError as e:
        result["error_type"], result["error"] = classify_aws_error(e)

    return result


@handle_aws_errors
def main() -> int:
    """Create a VPC for testing and output JSON result.

    Parses CLI arguments (--name, --region, --cidr) and creates a VPC
    with the specified configuration. Errors are handled by the decorator.

    Returns:
        0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(description="Create VPC for testing")
    parser.add_argument("--name", default="isv-test-vpc", help="VPC name prefix")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.0.0.0/16", help="VPC CIDR block")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result = create_vpc(ec2, args.name, args.cidr)
    result["region"] = args.region
    result["name"] = args.name

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
