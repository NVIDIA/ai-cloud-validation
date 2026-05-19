#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Test VPC CRUD operations on zCompute.

Adapted from providers/aws/scripts/network/vpc_crud_test.py.
zCompute-specific change: replace vpc_available waiter with poll loop.
"""

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Add both zcompute and aws common to path (errors.py lives in aws/scripts/common)
_here = __import__("pathlib").Path(__file__).resolve()
sys.path.insert(0, str(_here.parents[1]))           # providers/zcompute/scripts
sys.path.insert(0, str(_here.parents[3] / "aws" / "scripts"))  # providers/aws/scripts
from common.client import get_client   # verify=False already handled
from common.errors import delete_with_retry, handle_aws_errors


def _poll_vpc_available(ec2: Any, vpc_id: str, timeout: int = 120, interval: int = 5) -> str:
    """Return VPC state once available, or last known state on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_vpcs(VpcIds=[vpc_id])
        state = resp["Vpcs"][0]["State"]
        if state == "available":
            return state
        time.sleep(interval)
    # Return current state even if not available
    resp = ec2.describe_vpcs(VpcIds=[vpc_id])
    return resp["Vpcs"][0]["State"]


def test_create_vpc(ec2: Any, cidr: str, name: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        vpc = ec2.create_vpc(CidrBlock=cidr)
        vpc_id = vpc["Vpc"]["VpcId"]
        result["vpc_id"] = vpc_id
        result["cidr"] = cidr
        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[{"Key": "Name", "Value": name}, {"Key": "CreatedBy", "Value": "isvtest"}],
        )
        result["passed"] = True
        result["message"] = f"Created VPC {vpc_id}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_read_vpc(ec2: Any, vpc_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        # Poll instead of waiter (zCompute does not support boto3 waiters)
        state = _poll_vpc_available(ec2, vpc_id)

        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpc = response["Vpcs"][0]
        result["state"] = vpc["State"]
        result["cidr"] = vpc["CidrBlock"]
        result["is_default"] = vpc["IsDefault"]

        try:
            dns_support = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsSupport")
            dns_hostnames = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsHostnames")
            result["dns_support"] = dns_support["EnableDnsSupport"]["Value"]
            result["dns_hostnames"] = dns_hostnames["EnableDnsHostnames"]["Value"]
        except ClientError:
            result["dns_support"] = None
            result["dns_hostnames"] = None

        result["passed"] = state == "available"
        result["message"] = f"VPC {vpc_id} is {state}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_update_tags(ec2: Any, vpc_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[{"Key": "UpdateTest", "Value": "success"}, {"Key": "Timestamp", "Value": str(int(time.time()))}],
        )
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        tags = {t["Key"]: t["Value"] for t in response["Vpcs"][0].get("Tags", [])}
        if "UpdateTest" in tags and tags["UpdateTest"] == "success":
            result["passed"] = True
            result["tags_added"] = ["UpdateTest", "Timestamp"]
            result["message"] = "Tags updated successfully"
        else:
            result["error"] = "Tags not found after update"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_update_dns(ec2: Any, vpc_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        result["passed"] = True
        result["dns_hostnames"] = True
        result["dns_support"] = True
        result["message"] = "DNS settings updated successfully"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_delete_vpc(ec2: Any, vpc_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.delete_vpc(VpcId=vpc_id)
        time.sleep(2)
        try:
            ec2.describe_vpcs(VpcIds=[vpc_id])
            result["error"] = "VPC still exists after deletion"
        except ClientError as e:
            if "InvalidVpcID.NotFound" in str(e) or "InvalidVpc.NotFound" in str(e):
                result["passed"] = True
                result["message"] = f"VPC {vpc_id} deleted successfully"
            else:
                result["error"] = str(e)
    except ClientError as e:
        result["error"] = str(e)
    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--cidr", default="10.99.0.0/16")
    args = parser.parse_args()

    ec2 = get_client("ec2", region=args.region)
    suffix = str(uuid.uuid4())[:8]
    vpc_name = f"isv-crud-test-{suffix}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
        "vpc_name": vpc_name,
    }

    vpc_id = None
    try:
        create_result = test_create_vpc(ec2, args.cidr, vpc_name)
        result["tests"]["create_vpc"] = create_result
        if not create_result["passed"]:
            result["error"] = "Failed to create VPC"
            print(json.dumps(result, indent=2))
            return 1

        vpc_id = create_result["vpc_id"]
        result["network_id"] = vpc_id

        result["tests"]["read_vpc"] = test_read_vpc(ec2, vpc_id)
        result["tests"]["update_tags"] = test_update_tags(ec2, vpc_id)
        result["tests"]["update_dns"] = test_update_dns(ec2, vpc_id)

        delete_result = test_delete_vpc(ec2, vpc_id)
        result["tests"]["delete_vpc"] = delete_result
        if delete_result["passed"]:
            vpc_id = None

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        if vpc_id:
            delete_with_retry(ec2.delete_vpc, VpcId=vpc_id, resource_desc=f"VPC {vpc_id}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
