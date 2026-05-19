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

"""Verify an EC2 instance has been terminated after teardown.

Post-teardown sanitization check: confirms the instance is in
'terminated' state or no longer exists (already cleaned up by AWS).
Also verifies that the associated security group and key pair have
been removed.

Usage:
    python verify_terminated.py --instance-id i-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "state": "terminated",
    "resources_destroyed": true,
    "checks": {
        "instance": "terminated",
        "security_group": "deleted",
        "key_pair": "deleted"
    }
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
    """Verify instance and associated resources have been cleaned up.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Verify instance terminated")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--security-group-id", nargs="?", default=None, help="Security group ID to verify deleted")
    parser.add_argument("--key-name", nargs="?", default=None, help="Key pair name to verify deleted")
    args = parser.parse_args()

    # Treat empty strings (from unresolved Jinja2 templates) as None
    if args.security_group_id is not None and not args.security_group_id.strip():
        args.security_group_id = None
    if args.key_name is not None and not args.key_name.strip():
        args.key_name = None

    # Skip verification when teardown was intentionally skipped (dev workflow)
    if os.environ.get("AWS_BM_SKIP_TEARDOWN") == "true":
        print(
            json.dumps(
                {
                    "success": True,
                    "platform": "bm",
                    "instance_id": args.instance_id,
                    "message": "Verification skipped (AWS_BM_SKIP_TEARDOWN=true)",
                    "checks": {},
                },
                indent=2,
            )
        )
        return 0

    ec2 = boto3.client("ec2", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "region": args.region,
        "resources_destroyed": False,
        "checks": {},
    }

    issues: list[str] = []

    # Check instance state
    try:
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        state = instance["State"]["Name"]
        result["state"] = state

        if state in ("terminated", "shutting-down"):
            result["checks"]["instance"] = state
        else:
            result["checks"]["instance"] = state
            issues.append(f"Instance {args.instance_id} is {state}, expected terminated")
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
            result["state"] = "not_found"
            result["checks"]["instance"] = "not_found"
        else:
            issues.append(f"Error checking instance: {e}")

    # Check security group was deleted
    if args.security_group_id:
        try:
            ec2.describe_security_groups(GroupIds=[args.security_group_id])
            result["checks"]["security_group"] = "exists"
            issues.append(f"Security group {args.security_group_id} still exists")
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidGroup.NotFound":
                result["checks"]["security_group"] = "deleted"
            else:
                issues.append(f"Error checking SG: {e}")

    # Check key pair was deleted
    if args.key_name:
        try:
            ec2.describe_key_pairs(KeyNames=[args.key_name])
            result["checks"]["key_pair"] = "exists"
            issues.append(f"Key pair {args.key_name} still exists")
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
                result["checks"]["key_pair"] = "deleted"
            else:
                issues.append(f"Error checking key pair: {e}")

    if issues:
        result["error"] = "; ".join(issues)
    else:
        result["success"] = True
        result["resources_destroyed"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
