#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Create zCompute VPC with subnets for network testing.

Adapted from providers/aws/scripts/network/create_vpc.py.
zCompute-specific changes:
  - No boto3 waiters — uses poll loop for VPC 'available' state.
  - Single AZ ('symphony') — only one subnet created.
  - MapPublicIpOnLaunch silently ignored if unsupported.
  - Endpoint routed via AWS_ENDPOINT_URL_EC2 env var.
"""

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Add both zcompute and aws common to path (errors.py lives in aws/scripts/common)
_here = __import__("pathlib").Path(__file__).resolve()
sys.path.insert(0, str(_here.parents[1]))  # providers/zcompute/scripts (has client.py + errors.py)
from common.client import get_client   # verify=False already handled
from common.errors import classify_aws_error


def _poll_vpc_available(ec2: Any, vpc_id: str, timeout: int = 120, interval: int = 5) -> None:
    """Poll until VPC reaches 'available' state (boto3 waiters not supported by zCompute)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_vpcs(VpcIds=[vpc_id])
        state = resp["Vpcs"][0]["State"]
        if state == "available":
            return
        print(f"[create_vpc] VPC {vpc_id} state={state}, waiting ...", file=sys.stderr)
        time.sleep(interval)
    raise RuntimeError(f"VPC {vpc_id} did not reach 'available' within {timeout}s")


def create_vpc(ec2: Any, name: str, cidr: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "network_id": None,
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
        result["network_id"] = vpc_id

        # Poll for available state (no waiters in zCompute)
        _poll_vpc_available(ec2, vpc_id)

        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {"Key": "Name", "Value": f"{name}-{tag_suffix}"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        # Enable DNS hostnames (ignore if unsupported)
        try:
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
        except ClientError:
            pass

        # Create Internet Gateway
        igw = ec2.create_internet_gateway()
        igw_id = igw["InternetGateway"]["InternetGatewayId"]
        result["internet_gateway_id"] = igw_id
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        ec2.create_tags(
            Resources=[igw_id],
            Tags=[{"Key": "Name", "Value": f"{name}-igw-{tag_suffix}"}, {"Key": "CreatedBy", "Value": "isvtest"}],
        )

        # Single AZ in zCompute (symphony)
        azs = ec2.describe_availability_zones()
        az_name = azs["AvailabilityZones"][0]["ZoneName"]
        subnet_cidr = "10.0.1.0/24"

        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=az_name)
        subnet_id = subnet["Subnet"]["SubnetId"]
        ec2.create_tags(
            Resources=[subnet_id],
            Tags=[{"Key": "Name", "Value": f"{name}-subnet-{tag_suffix}"}, {"Key": "CreatedBy", "Value": "isvtest"}],
        )

        # MapPublicIpOnLaunch — ignore if unsupported
        try:
            ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
        except ClientError:
            pass

        desc = ec2.describe_subnets(SubnetIds=[subnet_id])
        available_ips = desc["Subnets"][0].get("AvailableIpAddressCount", 0)
        result["subnets"].append({
            "subnet_id": subnet_id,
            "cidr": subnet_cidr,
            "az": az_name,
            "auto_assign_public_ip": True,
            "available_ips": available_ips,
        })

        # Route table
        rtb = ec2.create_route_table(VpcId=vpc_id)
        rtb_id = rtb["RouteTable"]["RouteTableId"]
        result["route_table_id"] = rtb_id
        ec2.create_tags(
            Resources=[rtb_id],
            Tags=[{"Key": "Name", "Value": f"{name}-rtb-{tag_suffix}"}, {"Key": "CreatedBy", "Value": "isvtest"}],
        )
        ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)

        # Security group
        sg = ec2.create_security_group(
            GroupName=f"{name}-sg-{tag_suffix}",
            Description="Test security group",
            VpcId=vpc_id,
        )
        sg_id = sg["GroupId"]
        result["security_group_id"] = sg_id
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            ],
        )

        # DHCP options
        vpc_desc = ec2.describe_vpcs(VpcIds=[vpc_id])
        dhcp_options_id = vpc_desc["Vpcs"][0].get("DhcpOptionsId")
        result["dhcp_options"] = {
            "dhcp_options_id": dhcp_options_id or "default",
            "domain_name": "ec2.internal",
            "domain_name_servers": ["AmazonProvidedDNS"],
            "ntp_servers": [],
        }

        result["success"] = True

    except ClientError as e:
        result["error_type"], result["error"] = classify_aws_error(e)
    except Exception as e:
        result["error"] = str(e)

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="isv-test-vpc")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--cidr", default="10.0.0.0/16")
    args = parser.parse_args()

    ec2 = get_client("ec2", region=args.region)
    result = create_vpc(ec2, args.name, args.cidr)
    result["region"] = args.region
    result["name"] = args.name
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
