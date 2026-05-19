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

"""Test custom port security policies on virtual interfaces.

AWS mapping:
  Security groups attach to Elastic Network Interfaces. This probe creates
  a temporary VPC, subnet, target ENI, unrelated ENI, and SG. It applies a
  one-port ingress rule to the target ENI, verifies that exact port is
  allowed, verifies an adjacent port is not allowed, and verifies the SG is
  not attached to the unrelated ENI.

Usage:
    python sg_port_security_policy.py --region us-west-2 --allowed-port 8443
"""

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import ALREADY_GONE_CODES, handle_aws_errors
from common.vpc import cleanup_vpc_resources, create_test_vpc

CIDR = "10.84.0.0/16"
SUBNET_CIDR = "10.84.1.0/24"
POLICY_CIDR = "10.0.0.0/8"
ALREADY_GONE_CLEANUP_CODES = ALREADY_GONE_CODES | frozenset({"InvalidNetworkInterfaceID.NotFound"})
PORT_SECURITY_TEST_NAMES = [
    "create_virtual_interface",
    "apply_port_policy",
    "allowed_port_permitted",
    "unlisted_port_blocked",
    "other_interface_unaffected",
    "cleanup",
]


def _failed_port_security_results(error: str) -> dict[str, dict[str, Any]]:
    """Return a complete failed subtest contract for early setup failures."""
    return {name: {"passed": False, "error": error} for name in PORT_SECURITY_TEST_NAMES}


def _get_az(ec2: Any, region: str) -> str:
    """Return the first available AZ in the region."""
    azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"]
    if not azs:
        msg = f"No available AZ found in region {region}"
        raise ValueError(msg)
    return azs[0]["ZoneName"]


def _tcp_port_allowed(rules: list[dict[str, Any]], port: int, cidr: str = POLICY_CIDR) -> bool:
    """Return True when any TCP rule allows the requested port and CIDR."""
    for rule in rules:
        protocol = rule.get("IpProtocol")
        if protocol not in ("tcp", "-1"):
            continue
        from_port = int(rule.get("FromPort", 0))
        to_port = int(rule.get("ToPort", 65535))
        if not from_port <= port <= to_port:
            continue
        ranges = rule.get("IpRanges", [])
        if any(ip_range.get("CidrIp") == cidr for ip_range in ranges):
            return True
    return False


def _delete_with_dependency_retry(
    fn: Any,
    *,
    attempts: int = 3,
    delay: float = 1.0,
    **kwargs: Any,
) -> str | None:
    """Delete a resource that may have brief ENI or SG dependency lag."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            fn(**kwargs)
            return None
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ALREADY_GONE_CLEANUP_CODES:
                return None
            last_error = e
            if code == "DependencyViolation" and attempt < attempts:
                time.sleep(delay)
                continue
            return str(e)
    return str(last_error) if last_error else "delete did not complete"


def test_port_security_policy(
    ec2: Any,
    vpc_id: str,
    az: str,
    *,
    allowed_port: int,
) -> dict[str, Any]:
    """Verify a custom ingress port policy is scoped to one virtual interface."""
    results: dict[str, Any] = {}
    subnet_id = None
    sg_id = None
    eni_target = None
    eni_other = None
    cleanup_errors: list[str] = []
    adjacent_port = allowed_port + 1
    tag = f"isv-port-policy-{uuid.uuid4().hex[:6]}"
    expected_keys = PORT_SECURITY_TEST_NAMES[:-1]

    try:
        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=SUBNET_CIDR, AvailabilityZone=az)
        subnet_id = subnet["Subnet"]["SubnetId"]

        sg = ec2.create_security_group(
            GroupName=tag,
            Description="ISV port security policy test",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}],
                }
            ],
        )
        sg_id = sg["GroupId"]

        target = ec2.create_network_interface(SubnetId=subnet_id, Groups=[sg_id])
        eni_target = target["NetworkInterface"]["NetworkInterfaceId"]
        other = ec2.create_network_interface(SubnetId=subnet_id)
        eni_other = other["NetworkInterface"]["NetworkInterfaceId"]
        results["create_virtual_interface"] = {"passed": True}

        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": allowed_port,
                    "ToPort": allowed_port,
                    "IpRanges": [{"CidrIp": POLICY_CIDR}],
                }
            ],
        )
        results["apply_port_policy"] = {"passed": True}

        sg_info = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        rules = sg_info.get("IpPermissions", [])
        if _tcp_port_allowed(rules, allowed_port):
            results["allowed_port_permitted"] = {"passed": True, "message": f"TCP/{allowed_port} is allowed"}
        else:
            results["allowed_port_permitted"] = {"passed": False, "error": f"TCP/{allowed_port} is not allowed"}

        if not _tcp_port_allowed(rules, adjacent_port):
            results["unlisted_port_blocked"] = {"passed": True, "message": f"TCP/{adjacent_port} is not allowed"}
        else:
            results["unlisted_port_blocked"] = {"passed": False, "error": f"TCP/{adjacent_port} is allowed"}

        enis_info = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_target, eni_other])
        by_id = {eni["NetworkInterfaceId"]: eni for eni in enis_info["NetworkInterfaces"]}
        target_sgs = [group["GroupId"] for group in by_id[eni_target].get("Groups", [])]
        other_sgs = [group["GroupId"] for group in by_id[eni_other].get("Groups", [])]
        if sg_id not in target_sgs:
            results["other_interface_unaffected"] = {
                "passed": False,
                "error": "Port policy SG not attached to target interface",
            }
        elif sg_id in other_sgs:
            results["other_interface_unaffected"] = {
                "passed": False,
                "error": "Port policy leaked to unrelated interface",
            }
        else:
            results["other_interface_unaffected"] = {
                "passed": True,
                "message": "Port policy SG attached only to target interface",
            }

    except ClientError as e:
        for key in expected_keys:
            results.setdefault(key, {"passed": False, "error": str(e)})
    finally:
        for eni_id in [eni_target, eni_other]:
            if eni_id:
                error = _delete_with_dependency_retry(ec2.delete_network_interface, NetworkInterfaceId=eni_id)
                if error:
                    cleanup_errors.append(f"delete ENI {eni_id}: {error}")
        if subnet_id:
            error = _delete_with_dependency_retry(ec2.delete_subnet, SubnetId=subnet_id)
            if error:
                cleanup_errors.append(f"delete subnet {subnet_id}: {error}")
        if sg_id:
            error = _delete_with_dependency_retry(ec2.delete_security_group, GroupId=sg_id)
            if error:
                cleanup_errors.append(f"delete SG {sg_id}: {error}")

    results["cleanup"] = {"passed": not cleanup_errors}
    if cleanup_errors:
        results["cleanup"]["error"] = "; ".join(cleanup_errors)
    return results


@handle_aws_errors
def main() -> int:
    """Run the port security policy test and emit JSON result."""
    parser = argparse.ArgumentParser(description="Test port security policy on virtual interfaces")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--allowed-port", type=int, default=8443)
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    suffix = uuid.uuid4().hex[:8]
    vpc_name = f"isv-port-security-{suffix}"
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sg_port_security_policy",
        "tests": {},
    }

    vpc_id = None
    try:
        vpc_result = create_test_vpc(ec2, CIDR, vpc_name)
        vpc_id = vpc_result.get("vpc_id")
        if not vpc_result["passed"]:
            detail = vpc_result.get("error")
            bootstrap_error = f"VPC creation failed: {detail}" if detail else "VPC creation failed"
            result["tests"] = _failed_port_security_results(bootstrap_error)
            result["error"] = bootstrap_error
            print(json.dumps(result, indent=2))
            return 1

        if not vpc_id:
            raise RuntimeError("VPC creation did not return a VPC ID")
        az = _get_az(ec2, args.region)
        result["tests"] = test_port_security_policy(ec2, vpc_id, az, allowed_port=args.allowed_port)
        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        if not result["tests"]:
            result["tests"] = _failed_port_security_results(str(e))
    finally:
        if vpc_id:
            cleanup_vpc_resources(ec2, vpc_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
