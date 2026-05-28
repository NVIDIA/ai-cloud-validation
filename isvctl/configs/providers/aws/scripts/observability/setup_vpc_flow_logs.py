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

"""Enable AWS VPC Flow Logs for the observability validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.errors import classify_aws_error, handle_aws_errors

OWNER_TAG = {"Key": "CreatedBy", "Value": "isvtest"}
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _safe_suffix(value: str) -> str:
    """Return an AWS-resource-safe suffix from an external identifier."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)[:64]


def _assume_role_policy() -> str:
    """Build the VPC Flow Logs trust policy document."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "vpc-flow-logs.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def _publish_policy(log_group_arn: str) -> str:
    """Build the CloudWatch Logs publish policy document."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogGroups",
                        "logs:DescribeLogStreams",
                    ],
                    "Resource": [log_group_arn, f"{log_group_arn}:*"],
                }
            ],
        }
    )


def _partition_from_role_arn(role_arn: str) -> str:
    """Return the AWS partition from an IAM role ARN."""
    parts = role_arn.split(":")
    return parts[1] if len(parts) > 1 and parts[1] else "aws"


def _role_is_owned(iam: Any, role_name: str) -> bool:
    """Return True when the existing IAM role is tagged as suite-owned."""
    response = iam.list_role_tags(RoleName=role_name)
    return any(
        tag.get("Key") == OWNER_TAG["Key"] and tag.get("Value") == OWNER_TAG["Value"]
        for tag in response.get("Tags", [])
    )


def setup_vpc_flow_logs(ec2: Any, logs: Any, iam: Any, *, vpc_id: str, region: str, name: str) -> dict[str, Any]:
    """Create CloudWatch Logs, IAM, and VPC Flow Log resources."""
    suffix = _safe_suffix(vpc_id)
    log_group_name = f"/aws/vpc/flowlogs/{name}-{suffix}"
    role_name = f"{name}-flow-logs-{suffix}"
    policy_name = f"{name}-publish-flow-logs"

    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "setup_vpc_flow_logs",
        "network_id": vpc_id,
        "region": region,
        "log_group_name": log_group_name,
        "role_name": role_name,
        "policy_name": policy_name,
        "flow_log_id": "",
        "log_destination": log_group_name,
        "role_arn": "",
        "traffic_type": "ALL",
    }

    try:
        logs.create_log_group(logGroupName=log_group_name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "ResourceAlreadyExistsException":
            result["error_type"], result["error"] = classify_aws_error(e)
            return result

    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=_assume_role_policy(),
            Tags=[OWNER_TAG, {"Key": "Name", "Value": role_name}],
        )["Role"]
        role_arn = role["Arn"]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "EntityAlreadyExists":
            result["error_type"], result["error"] = classify_aws_error(e)
            return result
        try:
            if not _role_is_owned(iam, role_name):
                result["error_type"] = "resource_conflict"
                result["error"] = (
                    f"IAM role {role_name!r} already exists but is not tagged CreatedBy=isvtest; refusing to adopt"
                )
                return result
            role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        except ClientError as role_error:
            result["error_type"], result["error"] = classify_aws_error(role_error)
            return result

    result["role_arn"] = role_arn
    partition = _partition_from_role_arn(role_arn)
    account_id = role_arn.split(":")[4]
    log_group_arn = f"arn:{partition}:logs:{region}:{account_id}:log-group:{log_group_name}"
    try:
        iam.put_role_policy(RoleName=role_name, PolicyName=policy_name, PolicyDocument=_publish_policy(log_group_arn))
    except ClientError as e:
        result["error_type"], result["error"] = classify_aws_error(e)
        return result

    response: dict[str, Any] | None = None
    for attempt in range(1, 6):
        try:
            response = ec2.create_flow_logs(
                ResourceIds=[vpc_id],
                ResourceType="VPC",
                TrafficType="ALL",
                LogDestinationType="cloud-watch-logs",
                LogGroupName=log_group_name,
                DeliverLogsPermissionArn=role_arn,
                TagSpecifications=[
                    {
                        "ResourceType": "vpc-flow-log",
                        "Tags": [
                            {"Key": "CreatedBy", "Value": "isvtest"},
                            {"Key": "Name", "Value": name},
                        ],
                    }
                ],
            )
            break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"AccessDenied", "InvalidParameter", "InvalidParameterValue"} and attempt < 5:
                time.sleep(3 * attempt)
                continue
            result["error_type"], result["error"] = classify_aws_error(e)
            return result

    if response is None:
        result["error"] = "create_flow_logs did not return a response"
        return result

    flow_log_ids = response.get("FlowLogIds", [])
    result["flow_log_id"] = flow_log_ids[0] if flow_log_ids else ""
    result["success"] = bool(result["flow_log_id"])
    if not result["success"]:
        result["error"] = "create_flow_logs returned no FlowLogIds"
    return result


def _demo_result(*, vpc_id: str, region: str, name: str) -> dict[str, Any]:
    """Return deterministic setup evidence for demo-mode runs."""
    suffix = _safe_suffix(vpc_id)
    log_group_name = f"/aws/vpc/flowlogs/{name}-{suffix}"
    role_name = f"{name}-flow-logs-{suffix}"
    return {
        "success": True,
        "platform": "observability",
        "test_name": "setup_vpc_flow_logs",
        "network_id": vpc_id,
        "region": region,
        "log_group_name": log_group_name,
        "role_name": role_name,
        "policy_name": f"{name}-publish-flow-logs",
        "flow_log_id": "fl-demo",
        "log_destination": log_group_name,
        "role_arn": f"arn:aws:iam::000000000000:role/{role_name}",
        "traffic_type": "ALL",
    }


@handle_aws_errors
def main() -> int:
    """Enable VPC Flow Logs and emit setup evidence as JSON."""
    parser = argparse.ArgumentParser(description="Enable AWS VPC Flow Logs")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--name", default="isv-observability")
    args = parser.parse_args()

    if DEMO_MODE:
        print(json.dumps(_demo_result(vpc_id=args.vpc_id, region=args.region, name=args.name), indent=2))
        return 0

    result = setup_vpc_flow_logs(
        boto3.client("ec2", region_name=args.region),
        boto3.client("logs", region_name=args.region),
        boto3.client("iam"),
        vpc_id=args.vpc_id,
        region=args.region,
        name=args.name,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
