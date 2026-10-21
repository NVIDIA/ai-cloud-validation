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

"""Read one deployed AWS allow rule and a separately supplied approval export.

This collector never changes policy or creates approval records. The approval file
must come from the operator's trusted approval system; SG tags provide ownership
only. See docs/aws-network.md for the provider-neutral evidence contract.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


def collect_rule(ec2: Any, rule_id: str, region: str, owner_tag: str) -> dict[str, Any]:
    """Read the selected rule and require its group on an in-use network interface."""
    rules = ec2.describe_security_group_rules(SecurityGroupRuleIds=[rule_id])["SecurityGroupRules"]
    if len(rules) != 1 or rules[0].get("SecurityGroupRuleId") != rule_id:
        raise ValueError("Expected exactly the requested effective rule")
    rule = rules[0]
    group = rule["GroupId"]
    active = False
    for page in ec2.get_paginator("describe_network_interfaces").paginate(
        Filters=[{"Name": "group-id", "Values": [group]}, {"Name": "status", "Values": ["in-use"]}]
    ):
        if any(
            eni.get("Status") == "in-use" and any(g.get("GroupId") == group for g in eni.get("Groups", []))
            for eni in page.get("NetworkInterfaces", [])
        ):
            active = True
            break
    if not active:
        raise ValueError("Rule is not applied to an in-use network interface")
    # Qualify the policy with account and region so a record cannot match another environment.
    policy = f"aws:{region}:{rule['GroupOwnerId']}:security-group/{group}"
    peers = [key for key in ("CidrIpv4", "CidrIpv6", "PrefixListId", "ReferencedGroupInfo") if rule.get(key)]
    if len(peers) != 1:
        raise ValueError("Rule requires exactly one recognized peer selector")
    key = peers[0]
    if key in ("CidrIpv4", "CidrIpv6"):
        peer = "cidr:" + str(ipaddress.ip_network(rule[key], strict=False))
    elif key == "PrefixListId":
        # Membership can change independently of the rule and its approval.
        raise ValueError("Prefix-list rules require a provider adapter that verifies approved membership")
    else:
        ref = rule[key]
        peer = f"aws:{region}:{ref['UserId']}:security-group/{ref['GroupId']}"
    if type(rule.get("IsEgress")) is not bool:
        raise ValueError("Rule is missing its traffic direction")
    protocol = {"6": "tcp", "17": "udp", "1": "icmp", "58": "icmpv6", "-1": "all"}.get(
        rule["IpProtocol"], rule["IpProtocol"]
    )
    scope: dict[str, Any] = {
        "direction": "egress" if rule["IsEgress"] else "ingress",
        "source": policy if rule["IsEgress"] else peer,
        "destination": peer if rule["IsEgress"] else policy,
        "protocol": protocol,
    }
    if protocol in ("tcp", "udp"):
        scope["port_range"] = [rule["FromPort"], rule["ToPort"]]
    elif protocol in ("icmp", "icmpv6"):
        scope.update(icmp_type=rule["FromPort"], icmp_code=rule["ToPort"])
    return {
        "rule_id": rule_id,
        "policy_id": policy,
        "owner": next((t.get("Value", "") for t in rule.get("Tags", []) if t.get("Key") == owner_tag), ""),
        "effective": True,
        "action": "allow",
        "scope": scope,
    }


def main() -> int:
    """Collect live rule state and an independent record; leave approval grading to isvtest."""
    parser = argparse.ArgumentParser(description="Read effective allow rule and approval evidence (SEC30-03)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--rule-id", default="")
    parser.add_argument("--approval-file", default="")
    parser.add_argument("--owner-tag", default="Owner")
    args = parser.parse_args()
    result: dict[str, Any] = {"success": False, "platform": "network", "test_name": "allow_rule_approval"}
    try:
        if not args.rule_id or not args.approval_file or not args.owner_tag:
            raise ValueError("Configure --rule-id and --approval-file; --owner-tag must be non-empty")
        approval = json.loads(Path(args.approval_file).read_text())
        if not isinstance(approval, dict):
            raise ValueError("Approval export must be an object")
        ec2 = boto3.client("ec2", region_name=args.region)
        result["effective_rule"] = collect_rule(ec2, args.rule_id, args.region, args.owner_tag)
        result["approval"] = approval
        result["observed_at"] = datetime.now(UTC).isoformat()
        result["success"] = True
    except (BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError) as exc:
        result["error"] = str(exc)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
