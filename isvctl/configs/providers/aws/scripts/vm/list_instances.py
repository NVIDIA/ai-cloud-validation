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

"""List EC2 instances in a VPC.

Usage:
    python list_instances.py --vpc-id vpc-xxx --region us-west-2
    python list_instances.py --vpc-id vpc-xxx --instance-id i-xxx

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instances": [
        {
            "instance_id": "i-xxx",
            "instance_type": "g5.xlarge",
            "state": "running",
            "public_ip": "54.x.x.x",
            "private_ip": "10.0.1.5",
            "vpc_id": "vpc-xxx"
        }
    ],
    "count": 1,
    "found_target": true,
    "target_instance": "i-xxx"
}
"""

import argparse
import json
import os
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser(description="List EC2 instances in a VPC")
    parser.add_argument("--vpc-id", required=True, help="VPC ID to list instances for")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--instance-id", help="Target instance ID to verify exists in list")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instances": [],
    }

    try:
        response = ec2.describe_instances(
            Filters=[
                {"Name": "vpc-id", "Values": [args.vpc_id]},
            ]
        )

        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                state = instance["State"]["Name"]
                if state == "terminated":
                    continue
                result["instances"].append(
                    {
                        "instance_id": instance["InstanceId"],
                        "instance_type": instance.get("InstanceType", "unknown"),
                        "state": state,
                        "public_ip": instance.get("PublicIpAddress"),
                        "private_ip": instance.get("PrivateIpAddress"),
                        "vpc_id": instance.get("VpcId"),
                    }
                )

        result["count"] = len(result["instances"])

        if args.instance_id:
            result["target_instance"] = args.instance_id
            result["found_target"] = any(i["instance_id"] == args.instance_id for i in result["instances"])

        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
