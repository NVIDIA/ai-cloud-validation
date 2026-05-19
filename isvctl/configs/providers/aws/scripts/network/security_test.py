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

"""Test security group and NACL blocking rules (negative tests).

Usage:
    python security_test.py --region us-west-2 --cidr 10.94.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_vpc": {"passed": true},
        "sg_default_deny_inbound": {"passed": true},
        "sg_allows_specific_ssh": {"passed": true},
        "sg_denies_vpc_icmp": {"passed": true},
        "nacl_explicit_deny": {"passed": true},
        "default_nacl_allows_inbound": {"passed": true},
        "sg_restricted_egress": {"passed": true}
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
from common.vpc import cleanup_vpc_resources, create_test_vpc


def test_sg_default_deny_inbound(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test that an empty security group denies all inbound by default."""
    result = {"passed": False}

    try:
        # Create a security group with no inbound rules
        sg = ec2.create_security_group(
            GroupName=f"isv-test-empty-sg-{uuid.uuid4().hex[:8]}",
            Description="Test empty SG for default deny",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}]}],
        )
        sg_id = sg["GroupId"]

        # Remove default outbound rule for a clean slate
        ec2.revoke_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        )

        # Verify no inbound rules
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        sg_info = response["SecurityGroups"][0]
        inbound_rules = sg_info.get("IpPermissions", [])

        if not inbound_rules:
            result["passed"] = True
            result["message"] = "Empty SG has no inbound rules (default deny)"
        else:
            result["error"] = f"SG has unexpected inbound rules: {inbound_rules}"

        result["sg_id"] = sg_id
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_sg_allows_specific_ssh(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test that SG can allow SSH from specific CIDR only."""
    result = {"passed": False}

    try:
        # Create SG that only allows SSH from specific CIDR
        sg = ec2.create_security_group(
            GroupName=f"isv-test-ssh-sg-{uuid.uuid4().hex[:8]}",
            Description="Test SG allowing specific SSH",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}]}],
        )
        sg_id = sg["GroupId"]

        # Add SSH rule for specific CIDR
        allowed_cidr = "192.168.1.0/24"
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": allowed_cidr, "Description": "SSH from specific CIDR"}],
                }
            ],
        )

        # Verify the rule
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        sg_info = response["SecurityGroups"][0]
        inbound_rules = sg_info.get("IpPermissions", [])

        ssh_rule_found = False
        for rule in inbound_rules:
            if rule.get("FromPort") == 22 and rule.get("ToPort") == 22:
                for ip_range in rule.get("IpRanges", []):
                    if ip_range.get("CidrIp") == allowed_cidr:
                        ssh_rule_found = True
                        break

        if ssh_rule_found:
            result["passed"] = True
            result["message"] = f"SG allows SSH from {allowed_cidr} only"
            result["allowed_cidr"] = allowed_cidr
        else:
            result["error"] = "SSH rule not found or incorrect"

        result["sg_id"] = sg_id
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_sg_denies_vpc_icmp(ec2: Any, vpc_id: str, vpc_cidr: str) -> dict[str, Any]:
    """Test that SG without ICMP rule denies ICMP from VPC."""
    result = {"passed": False}

    try:
        # Create SG without ICMP rules
        sg = ec2.create_security_group(
            GroupName=f"isv-test-no-icmp-sg-{uuid.uuid4().hex[:8]}",
            Description="Test SG without ICMP",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}]}],
        )
        sg_id = sg["GroupId"]

        # Verify no ICMP rules
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        sg_info = response["SecurityGroups"][0]
        inbound_rules = sg_info.get("IpPermissions", [])

        icmp_allowed = False
        for rule in inbound_rules:
            if rule.get("IpProtocol") == "icmp" or rule.get("IpProtocol") == "-1":
                for ip_range in rule.get("IpRanges", []):
                    cidr = ip_range.get("CidrIp", "")
                    if cidr == "0.0.0.0/0" or cidr == vpc_cidr:
                        icmp_allowed = True
                        break

        if not icmp_allowed:
            result["passed"] = True
            result["message"] = "SG denies ICMP from VPC (not explicitly allowed)"
        else:
            result["error"] = "ICMP is unexpectedly allowed"

        result["sg_id"] = sg_id
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_nacl_explicit_deny(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test creating NACL with explicit deny rule."""
    result = {"passed": False}

    try:
        # Create custom NACL
        nacl = ec2.create_network_acl(VpcId=vpc_id)
        nacl_id = nacl["NetworkAcl"]["NetworkAclId"]

        ec2.create_tags(
            Resources=[nacl_id],
            Tags=[
                {"Key": "Name", "Value": "isv-test-deny-nacl"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        # Add explicit deny rule for ICMP from 10.0.0.0/8
        ec2.create_network_acl_entry(
            NetworkAclId=nacl_id,
            RuleNumber=100,
            Protocol="1",  # ICMP
            RuleAction="deny",
            Egress=False,
            CidrBlock="10.0.0.0/8",
            IcmpTypeCode={"Code": -1, "Type": -1},
        )

        # Verify the rule
        response = ec2.describe_network_acls(NetworkAclIds=[nacl_id])
        nacl_info = response["NetworkAcls"][0]
        entries = nacl_info.get("Entries", [])

        deny_rule_found = False
        for entry in entries:
            if (
                entry.get("RuleNumber") == 100
                and entry.get("RuleAction") == "deny"
                and entry.get("CidrBlock") == "10.0.0.0/8"
                and not entry.get("Egress")
            ):
                deny_rule_found = True
                break

        if deny_rule_found:
            result["passed"] = True
            result["message"] = "NACL has explicit deny rule for ICMP from 10.0.0.0/8"
        else:
            result["error"] = "Deny rule not found"

        result["nacl_id"] = nacl_id
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_default_nacl_allows_inbound(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test that default NACL allows all inbound (for comparison)."""
    result = {"passed": False}

    try:
        response = ec2.describe_network_acls(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "default", "Values": ["true"]},
            ]
        )

        if not response["NetworkAcls"]:
            result["error"] = "Default NACL not found"
            return result

        nacl = response["NetworkAcls"][0]
        entries = nacl.get("Entries", [])

        # Look for allow-all inbound rule
        allow_all_inbound = False
        for entry in entries:
            if (
                entry.get("RuleAction") == "allow"
                and entry.get("CidrBlock") == "0.0.0.0/0"
                and not entry.get("Egress")
                and entry.get("Protocol") == "-1"
            ):
                allow_all_inbound = True
                break

        if allow_all_inbound:
            result["passed"] = True
            result["message"] = "Default NACL has allow-all inbound rule (for comparison)"
        else:
            result["error"] = "Default NACL doesn't have expected allow-all rule"

        result["nacl_id"] = nacl["NetworkAclId"]
    except ClientError as e:
        result["error"] = str(e)

    return result


def test_sg_restricted_egress(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Test that SG can restrict egress to HTTPS only."""
    result = {"passed": False}

    try:
        # Create SG with restricted egress
        sg = ec2.create_security_group(
            GroupName=f"isv-test-egress-sg-{uuid.uuid4().hex[:8]}",
            Description="Test SG with restricted egress",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}]}],
        )
        sg_id = sg["GroupId"]

        # Remove default egress
        ec2.revoke_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        )

        # Add HTTPS-only egress
        ec2.authorize_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS only"}],
                }
            ],
        )

        # Verify
        response = ec2.describe_security_groups(GroupIds=[sg_id])
        sg_info = response["SecurityGroups"][0]
        egress_rules = sg_info.get("IpPermissionsEgress", [])

        https_only = len(egress_rules) == 1
        if egress_rules:
            rule = egress_rules[0]
            https_only = https_only and rule.get("FromPort") == 443 and rule.get("ToPort") == 443

        if https_only:
            result["passed"] = True
            result["message"] = "SG with restricted egress allows only HTTPS outbound"
        else:
            result["error"] = f"Unexpected egress rules: {egress_rules}"

        result["sg_id"] = sg_id
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test security blocking rules")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.94.0.0/16", help="CIDR for test VPC")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    suffix = str(uuid.uuid4())[:8]
    vpc_name = f"isv-security-test-{suffix}"

    result = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    vpc_id = None
    sg_ids = []
    nacl_ids = []

    try:
        # Create test VPC
        vpc_result = create_test_vpc(ec2, args.cidr, vpc_name)
        result["tests"]["create_vpc"] = vpc_result

        if not vpc_result["passed"]:
            print(json.dumps(result, indent=2))
            return 1

        vpc_id = vpc_result["vpc_id"]
        result["network_id"] = vpc_id

        # Test 1: Empty SG default deny
        test1 = test_sg_default_deny_inbound(ec2, vpc_id)
        result["tests"]["sg_default_deny_inbound"] = test1
        if test1.get("sg_id"):
            sg_ids.append(test1["sg_id"])

        # Test 2: SG allows specific SSH
        test2 = test_sg_allows_specific_ssh(ec2, vpc_id)
        result["tests"]["sg_allows_specific_ssh"] = test2
        if test2.get("sg_id"):
            sg_ids.append(test2["sg_id"])

        # Test 3: SG denies VPC ICMP
        test3 = test_sg_denies_vpc_icmp(ec2, vpc_id, args.cidr)
        result["tests"]["sg_denies_vpc_icmp"] = test3
        if test3.get("sg_id"):
            sg_ids.append(test3["sg_id"])

        # Test 4: NACL explicit deny
        test4 = test_nacl_explicit_deny(ec2, vpc_id)
        result["tests"]["nacl_explicit_deny"] = test4
        if test4.get("nacl_id"):
            nacl_ids.append(test4["nacl_id"])

        # Test 5: Default NACL allows inbound
        test5 = test_default_nacl_allows_inbound(ec2, vpc_id)
        result["tests"]["default_nacl_allows_inbound"] = test5

        # Test 6: SG restricted egress
        test6 = test_sg_restricted_egress(ec2, vpc_id)
        result["tests"]["sg_restricted_egress"] = test6
        if test6.get("sg_id"):
            sg_ids.append(test6["sg_id"])

        # Check overall success
        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        # Cleanup
        if vpc_id:
            cleanup_vpc_resources(ec2, vpc_id, sg_ids=sg_ids, nacl_ids=nacl_ids)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
