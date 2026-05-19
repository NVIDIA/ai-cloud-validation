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

"""Retrieve user-defined tags on an AWS bare-metal EC2 instance.

Fetches all tags applied to the instance via the EC2 API and returns them
as a flat key->value dict. Required-key validation is handled by
InstanceTagCheck in the validation layer.

Usage:
    python describe_tags.py --instance-id i-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "tags": {
        "Name": "isv-bm-test-gpu",
        "CreatedBy": "isvtest"
    },
    "tag_count": 2
}
"""

import argparse
import json
import os
import sys
from typing import Any

import boto3


def main() -> int:
    """Retrieve EC2 bare-metal instance tags and print structured JSON output."""
    parser = argparse.ArgumentParser(description="Describe bare-metal EC2 instance tags")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "tags": {},
        "tag_count": 0,
    }

    try:
        response = ec2.describe_instances(InstanceIds=[args.instance_id])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {args.instance_id} not found"
            print(json.dumps(result, indent=2))
            return 1

        instance = reservations[0]["Instances"][0]
        raw_tags = instance.get("Tags", [])

        # Convert [{Key: k, Value: v}, ...] -> {k: v}
        tags = {t["Key"]: t["Value"] for t in raw_tags}
        result["tags"] = tags
        result["tag_count"] = len(tags)
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
