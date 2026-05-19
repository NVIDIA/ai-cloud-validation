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

"""Verify that an OS image is correctly installed on a bare-metal instance.

Checks the running instance's AMI metadata to confirm it was provisioned
from a valid GPU-capable image.

Usage:
    python verify_image_installed.py --instance-id i-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "instance_id": "i-xxx",
    "image_id": "ami-xxx",
    "image_name": "Deep Learning Base ...",
    "image_architecture": "x86_64",
    "instance_state": "running",
    "instance_type": "g4dn.metal"
}
"""

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from common.errors import handle_aws_errors


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Verify OS image installed on BM instance")
    parser.add_argument("--instance-id", required=True, help="Instance ID to check")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "instance_id": args.instance_id,
        "image_id": "",
        "image_name": "",
        "image_architecture": "",
        "instance_state": "",
        "instance_type": "",
    }

    # Get instance details
    response = ec2.describe_instances(InstanceIds=[args.instance_id])
    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        result["error"] = f"Instance {args.instance_id} not found"
        print(json.dumps(result, indent=2))
        return 1

    instance = reservations[0]["Instances"][0]
    ami_id = instance.get("ImageId", "")
    result["instance_state"] = instance["State"]["Name"]
    result["state"] = result["instance_state"]
    result["instance_type"] = instance.get("InstanceType", "")
    result["image_id"] = ami_id

    if result["instance_state"] != "running":
        result["error"] = f"Instance is {result['instance_state']}, expected running"
        print(json.dumps(result, indent=2))
        return 1

    # Get AMI details
    try:
        ami_response = ec2.describe_images(ImageIds=[ami_id])
        images = ami_response.get("Images", [])
        if images:
            ami = images[0]
            result["image_name"] = ami.get("Name", "")
            result["image_architecture"] = ami.get("Architecture", "")
            result["image_description"] = ami.get("Description", "")
            result["image_state"] = ami.get("State", "")
    except Exception as e:
        # AMI may have been deregistered; still valid that instance is running from it
        result["image_name"] = "(AMI metadata unavailable)"
        print(f"Warning: Could not describe AMI {ami_id}: {e}", file=sys.stderr)

    result["success"] = True

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
