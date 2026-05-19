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

"""VPC IP configuration test - AWS reference implementation.

Inspects VPC DHCP options, subnet CIDRs, and auto-assign IP settings.
Outputs structured JSON for the VpcIpConfigCheck validation.

Usage:
    python vpc_ip_config_test.py --vpc-id vpc-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "vpc_ip_config",
    "network_id": "vpc-xxx",
    "cidr": "10.0.0.0/16",
    "subnets": [
        {
            "subnet_id": "subnet-xxx",
            "cidr": "10.0.1.0/24",
            "az": "us-west-2a",
            "auto_assign_public_ip": true,
            "available_ips": 251
        }
    ],
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
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import classify_aws_error, handle_aws_errors


def describe_dhcp_options(ec2: Any, dhcp_options_id: str) -> dict[str, Any]:
    """Describe DHCP options set and extract configuration values."""
    response = ec2.describe_dhcp_options(DhcpOptionsIds=[dhcp_options_id])
    dhcp = response["DhcpOptions"][0]

    result: dict[str, Any] = {
        "dhcp_options_id": dhcp_options_id,
        "domain_name": None,
        "domain_name_servers": [],
        "ntp_servers": [],
    }

    for config in dhcp.get("DhcpConfigurations", []):
        key = config["Key"]
        values = [v["Value"] for v in config.get("Values", [])]

        if key == "domain-name":
            result["domain_name"] = values[0] if values else None
        elif key == "domain-name-servers":
            result["domain_name_servers"] = values
        elif key == "ntp-servers":
            result["ntp_servers"] = values

    return result


def describe_subnets(ec2: Any, vpc_id: str) -> list[dict[str, Any]]:
    """Describe all subnets in a VPC with IP config details."""
    response = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
    )

    subnets = []
    for subnet in response["Subnets"]:
        subnets.append(
            {
                "subnet_id": subnet["SubnetId"],
                "cidr": subnet["CidrBlock"],
                "az": subnet["AvailabilityZone"],
                "auto_assign_public_ip": subnet.get("MapPublicIpOnLaunch", False),
                "available_ips": subnet.get("AvailableIpAddressCount", 0),
            }
        )

    return subnets


@handle_aws_errors
def main() -> int:
    """Inspect VPC IP configuration and output JSON."""
    parser = argparse.ArgumentParser(description="VPC IP configuration test (AWS)")
    parser.add_argument("--vpc-id", required=True, help="VPC ID to inspect")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "vpc_ip_config",
        "network_id": args.vpc_id,
        "cidr": None,
        "subnets": [],
        "dhcp_options": None,
    }

    try:
        # Describe VPC to get CIDR and DHCP options ID
        vpc_response = ec2.describe_vpcs(VpcIds=[args.vpc_id])
        vpc = vpc_response["Vpcs"][0]
        result["cidr"] = vpc["CidrBlock"]
        dhcp_options_id = vpc.get("DhcpOptionsId")

        # Describe DHCP options
        if dhcp_options_id and dhcp_options_id != "default":
            result["dhcp_options"] = describe_dhcp_options(ec2, dhcp_options_id)
        else:
            # AWS default DHCP options
            result["dhcp_options"] = {
                "dhcp_options_id": dhcp_options_id or "default",
                "domain_name": "ec2.internal",
                "domain_name_servers": ["AmazonProvidedDNS"],
                "ntp_servers": [],
            }

        # Describe subnets
        result["subnets"] = describe_subnets(ec2, args.vpc_id)

        result["success"] = True

    except ClientError as e:
        result["error_type"], result["error"] = classify_aws_error(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
