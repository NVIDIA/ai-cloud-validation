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

"""Verify that an OS install config can provision a bare-metal instance.

Creates a Launch Template from the running instance's configuration, validates
it via a dry-run launch, then cleans up the template.

Usage:
    python verify_config_installable.py --instance-id i-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "instance_id": "i-xxx",
    "config_id": "lt-xxx",
    "config_name": "isvtest-bm-config-xxx",
    "dry_run_passed": true,
    "instance_state": "running"
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
from common.errors import handle_aws_errors


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Verify install config works for BM provisioning")
    parser.add_argument("--instance-id", required=True, help="Running BM instance to derive config from")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    config_name = f"isvtest-bm-config-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "instance_id": args.instance_id,
        "config_id": "",
        "config_name": config_name,
        "dry_run_passed": False,
        "instance_state": "",
    }

    lt_id = ""

    try:
        # Get running instance config
        response = ec2.describe_instances(InstanceIds=[args.instance_id])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {args.instance_id} not found"
            print(json.dumps(result, indent=2))
            return 1

        instance = reservations[0]["Instances"][0]
        result["instance_state"] = instance["State"]["Name"]

        ami_id = instance["ImageId"]
        instance_type = instance["InstanceType"]
        key_name = instance.get("KeyName", "")
        sg_ids = [sg["GroupId"] for sg in instance.get("SecurityGroups", [])]
        subnet_id = instance.get("SubnetId", "")

        # Create Launch Template from instance config
        lt_data: dict[str, Any] = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
        }
        if key_name:
            lt_data["KeyName"] = key_name
        if sg_ids:
            lt_data["SecurityGroupIds"] = sg_ids

        lt_response = ec2.create_launch_template(
            LaunchTemplateName=config_name,
            LaunchTemplateData=lt_data,
            TagSpecifications=[
                {
                    "ResourceType": "launch-template",
                    "Tags": [
                        {"Key": "Name", "Value": config_name},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        lt_id = lt_response["LaunchTemplate"]["LaunchTemplateId"]
        result["config_id"] = lt_id

        # Dry-run launch to validate the config is usable
        try:
            ec2.run_instances(
                LaunchTemplate={"LaunchTemplateId": lt_id},
                SubnetId=subnet_id,
                MinCount=1,
                MaxCount=1,
                DryRun=True,
            )
        except ClientError as e:
            # DryRun=True raises DryRunOperation on success
            if e.response["Error"]["Code"] == "DryRunOperation":
                result["dry_run_passed"] = True
            else:
                result["error"] = f"Dry-run failed: {e.response['Error']['Message']}"
                print(json.dumps(result, indent=2))
                return 1

        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)
    finally:
        # Always clean up the launch template
        if lt_id:
            try:
                ec2.delete_launch_template(LaunchTemplateId=lt_id)
                print(f"Cleaned up launch template {lt_id}", file=sys.stderr)
            except ClientError:
                pass

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
