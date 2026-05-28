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

"""Tear down AWS resources created for observability VPC Flow Logs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.errors import classify_aws_error, handle_aws_errors


def _delete_or_already_gone(fn: Any, *, gone_codes: set[str], **kwargs: Any) -> tuple[bool, dict[str, str] | None]:
    """Call a delete function and treat already-missing resources as deleted."""
    try:
        fn(**kwargs)
        return True, None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in gone_codes:
            return True, None
        error_type, error = classify_aws_error(e)
        return False, {"error_type": error_type, "error": error}


def _record_cleanup_error(
    result: dict[str, Any],
    *,
    resource_type: str,
    resource_id: str,
    error: dict[str, str],
) -> None:
    """Add a cleanup error to the teardown result."""
    result.setdefault("cleanup_errors", []).append(
        {
            "resource_type": resource_type,
            "resource_id": resource_id,
            **error,
        }
    )


def teardown_vpc_flow_logs(
    ec2: Any,
    logs: Any,
    iam: Any,
    *,
    flow_log_id: str,
    log_group_name: str,
    role_name: str,
    policy_name: str,
    skip_destroy: bool,
) -> dict[str, Any]:
    """Delete VPC Flow Log resources created by setup_vpc_flow_logs."""
    result: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "teardown_vpc_flow_logs",
        "resources_destroyed": False,
        "deleted": {
            "flow_log_id": "",
            "log_group_name": "",
            "role_policy": "",
            "role_name": "",
        },
    }
    if skip_destroy:
        result["message"] = "Destroy skipped (--skip-destroy flag or AWS_OBSERVABILITY_SKIP_TEARDOWN=true)"
        return result

    if flow_log_id:
        deleted, error = _delete_or_already_gone(
            ec2.delete_flow_logs,
            FlowLogIds=[flow_log_id],
            gone_codes={"InvalidFlowLogId.NotFound"},
        )
        if deleted:
            result["deleted"]["flow_log_id"] = flow_log_id
        elif error:
            _record_cleanup_error(result, resource_type="flow_log_id", resource_id=flow_log_id, error=error)

    if log_group_name:
        deleted, error = _delete_or_already_gone(
            logs.delete_log_group,
            logGroupName=log_group_name,
            gone_codes={"ResourceNotFoundException"},
        )
        if deleted:
            result["deleted"]["log_group_name"] = log_group_name
        elif error:
            _record_cleanup_error(result, resource_type="log_group_name", resource_id=log_group_name, error=error)

    if role_name and policy_name:
        deleted, error = _delete_or_already_gone(
            iam.delete_role_policy,
            RoleName=role_name,
            PolicyName=policy_name,
            gone_codes={"NoSuchEntity"},
        )
        if deleted:
            result["deleted"]["role_policy"] = policy_name
        elif error:
            _record_cleanup_error(result, resource_type="role_policy", resource_id=policy_name, error=error)

    if role_name:
        deleted, error = _delete_or_already_gone(
            iam.delete_role,
            RoleName=role_name,
            gone_codes={"NoSuchEntity"},
        )
        if deleted:
            result["deleted"]["role_name"] = role_name
        elif error:
            _record_cleanup_error(result, resource_type="role_name", resource_id=role_name, error=error)

    if result.get("cleanup_errors"):
        result["success"] = False
        result["error"] = "One or more VPC Flow Log teardown operations failed"
        return result

    result["resources_destroyed"] = True
    return result


@handle_aws_errors
def main() -> int:
    """Delete VPC Flow Log resources and emit teardown evidence as JSON."""
    parser = argparse.ArgumentParser(description="Tear down AWS observability Flow Log resources")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--flow-log-id", default="")
    parser.add_argument("--log-group-name", default="")
    parser.add_argument("--role-name", default="")
    parser.add_argument("--policy-name", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    skip_destroy = args.skip_destroy or os.environ.get("AWS_OBSERVABILITY_SKIP_TEARDOWN", "").lower() == "true"
    result = teardown_vpc_flow_logs(
        boto3.client("ec2", region_name=args.region),
        boto3.client("logs", region_name=args.region),
        boto3.client("iam"),
        flow_log_id=args.flow_log_id,
        log_group_name=args.log_group_name,
        role_name=args.role_name,
        policy_name=args.policy_name,
        skip_destroy=skip_destroy,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
