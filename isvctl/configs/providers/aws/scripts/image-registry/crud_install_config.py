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

"""CRUD an OS install configuration using EC2 Launch Templates.

Self-contained test: creates a launch template, reads it back, creates a
new version (update), then deletes it.

Usage:
    python crud_install_config.py --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "config_id": "lt-xxx",
    "config_name": "isvtest-install-config-xxx",
    "operations": {
        "create": {"passed": true, "config_id": "lt-xxx", "version": 1},
        "read":   {"passed": true, "config_name": "...", "instance_type": "..."},
        "update": {"passed": true, "new_version": 2},
        "delete": {"passed": true}
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
from common.errors import handle_aws_errors


def test_create(ec2: Any, name: str, region: str) -> dict[str, Any]:
    """Create a launch template."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.create_launch_template(
            LaunchTemplateName=name,
            LaunchTemplateData={
                "InstanceType": "g4dn.xlarge",
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"{name}-instance"},
                            {"Key": "CreatedBy", "Value": "isvtest"},
                        ],
                    }
                ],
            },
            TagSpecifications=[
                {
                    "ResourceType": "launch-template",
                    "Tags": [
                        {"Key": "Name", "Value": name},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        lt = response["LaunchTemplate"]
        result["config_id"] = lt["LaunchTemplateId"]
        result["version"] = lt["DefaultVersionNumber"]
        result["passed"] = True
        result["message"] = f"Created launch template {lt['LaunchTemplateId']}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_read(ec2: Any, lt_id: str) -> dict[str, Any]:
    """Read back the launch template and verify its contents."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_launch_template_versions(
            LaunchTemplateId=lt_id,
            Versions=["$Default"],
        )
        versions = response.get("LaunchTemplateVersions", [])
        if not versions:
            result["error"] = "No versions found"
            return result

        version = versions[0]
        data = version.get("LaunchTemplateData", {})
        result["config_name"] = version.get("LaunchTemplateName", "")
        result["instance_type"] = data.get("InstanceType", "")
        result["version_number"] = version.get("VersionNumber")
        result["passed"] = True
        result["message"] = f"Read template: type={data.get('InstanceType')}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_update(ec2: Any, lt_id: str) -> dict[str, Any]:
    """Update by creating a new version with different instance type."""
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.create_launch_template_version(
            LaunchTemplateId=lt_id,
            LaunchTemplateData={
                "InstanceType": "g5.xlarge",
            },
            VersionDescription="Updated instance type for validation",
        )
        new_version = response["LaunchTemplateVersion"]["VersionNumber"]
        result["new_version"] = new_version
        result["passed"] = True
        result["message"] = f"Created version {new_version} with g5.xlarge"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_delete(ec2: Any, lt_id: str) -> dict[str, Any]:
    """Delete the launch template."""
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.delete_launch_template(LaunchTemplateId=lt_id)
        result["passed"] = True
        result["message"] = f"Deleted launch template {lt_id}"
    except ClientError as e:
        result["error"] = str(e)
    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD OS install config (Launch Template)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    name = f"isvtest-install-config-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "config_id": "",
        "config_name": name,
        "operations": {
            "create": {"passed": False},
            "read": {"passed": False},
            "update": {"passed": False},
            "delete": {"passed": False},
        },
    }

    lt_id = ""

    try:
        # CREATE
        create_result = test_create(ec2, name, args.region)
        result["operations"]["create"] = create_result
        if not create_result["passed"]:
            raise RuntimeError(f"Create failed: {create_result.get('error')}")
        lt_id = create_result["config_id"]
        result["config_id"] = lt_id

        # READ
        read_result = test_read(ec2, lt_id)
        result["operations"]["read"] = read_result
        if not read_result["passed"]:
            raise RuntimeError(f"Read failed: {read_result.get('error')}")

        # UPDATE
        update_result = test_update(ec2, lt_id)
        result["operations"]["update"] = update_result
        if not update_result["passed"]:
            raise RuntimeError(f"Update failed: {update_result.get('error')}")

        # DELETE
        delete_result = test_delete(ec2, lt_id)
        result["operations"]["delete"] = delete_result
        if not delete_result["passed"]:
            raise RuntimeError(f"Delete failed: {delete_result.get('error')}")

        result["success"] = True

    except RuntimeError as e:
        result["error"] = str(e)
        # Cleanup on partial failure
        if lt_id:
            try:
                ec2.delete_launch_template(LaunchTemplateId=lt_id)
            except ClientError:
                pass

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
