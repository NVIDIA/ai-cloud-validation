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

"""Describe a running AWS EC2 bare-metal instance.

Lightweight test-phase step that fetches current instance state and
passes through SSH connection info. Validations (SSH, GPU, host OS)
bind to this step so they run in the test phase rather than setup,
ensuring teardown always runs even if tests fail.

Usage:
    python describe_instance.py --instance-id i-xxx --region us-west-2 \
        --key-file /tmp/key.pem

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "instance_type": "g4dn.metal",
    "state": "running",
    "public_ip": "54.x.x.x",
    "private_ip": "10.0.1.5",
    "key_file": "/tmp/key.pem",
    "ssh_user": "ubuntu"
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
    """Describe a bare-metal EC2 instance and return its current state.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Describe bare-metal EC2 instance")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "region": args.region,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
    }

    try:
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]

        result["state"] = instance["State"]["Name"]
        result["instance_type"] = instance.get("InstanceType")
        result["public_ip"] = instance.get("PublicIpAddress")
        result["private_ip"] = instance.get("PrivateIpAddress")
        result["vpc_id"] = instance.get("VpcId")
        result["subnet_id"] = instance.get("SubnetId")
        result["availability_zone"] = instance.get("Placement", {}).get("AvailabilityZone")
        result["launch_time"] = instance.get("LaunchTime", "").isoformat() if instance.get("LaunchTime") else None
        result["success"] = True

    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
            result["error"] = f"Instance {args.instance_id} not found"
        else:
            result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
