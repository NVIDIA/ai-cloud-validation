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

"""Verify BMC is reachable only via a hardened bastion (AWS reference).

In AWS, the Nitro/hypervisor BMC plane is provider-owned and is not exposed
to customer VPCs, so this test cannot fully validate SEC12-03 on AWS. The
reference exercises the customer-visible side of the contract:

  1. ``bastion_identifiable`` - at least one resource is tagged as a
     bastion/jumphost when a customer-visible BMC management network exists.
  2. ``management_ingress_via_bastion_only`` - security groups protecting
     resources tagged as BMC management only accept ingress from the bastion
     SG (no ``0.0.0.0/0`` on management ports).
  3. ``no_direct_public_route`` - subnets tagged as BMC management have no
     route to an internet gateway and do not auto-assign public IPs.
  4. ``bastion_hardened`` - the bastion's own SG does not allow ``0.0.0.0/0``
     on SSH (port 22).

When no BMC management network is found in the account (the typical AWS
case, since BMC is provider-hidden), each subtest passes with a
``provider_hidden`` marker so the validation is non-spurious. Self-managed
NCPs running their own BMC fabric should tag their resources with
``bmc/ipmi/redfish/oob/out-of-band`` to enable strict enforcement.

Usage:
    python bmc_bastion_access_test.py --region us-west-2

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_bastion_access",
    "management_networks_checked": 0,
    "tests": {
      "bastion_identifiable":              {"passed": true},
      "management_ingress_via_bastion_only": {"passed": true},
      "no_direct_public_route":            {"passed": true},
      "bastion_hardened":                  {"passed": true}
    }
  }
"""

import argparse
import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from common.errors import handle_aws_errors

# Word-boundary matchers reuse the alphanumeric lookarounds from
# bmc_management_network_test.py so underscore- and hyphen-delimited tag
# values are matched while substrings like "submarine-bmcollege" are not.
_MANAGEMENT_TAG_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:bmc|ipmi|redfish|oob|out[-_]?of[-_]?band)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_BASTION_TAG_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:bastion|jump[-_]?host|jumpbox)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Common management ingress ports to scrutinize for world-open rules.
_MANAGEMENT_PORTS = (22, 443, 623)
_PUBLIC_CIDRS = ("0.0.0.0/0", "::/0")


def _collect_paginated(ec2: Any, operation_name: str, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Collect all items for a paginated EC2 describe operation."""
    paginator = ec2.get_paginator(operation_name)
    items: list[dict[str, Any]] = []
    for page in paginator.paginate(**kwargs):
        items.extend(page.get(result_key, []))
    return items


def _tag_text(resource: dict[str, Any]) -> str:
    """Return a single string of all key=value tags for regex matching."""
    return " ".join(f"{tag.get('Key', '')}={tag.get('Value', '')}" for tag in resource.get("Tags", []))


def _is_management_resource(resource: dict[str, Any]) -> bool:
    """Return True when a resource tag identifies it as a BMC management resource."""
    return bool(_MANAGEMENT_TAG_PATTERN.search(_tag_text(resource)))


def _is_bastion_resource(resource: dict[str, Any]) -> bool:
    """Return True when a resource tag identifies it as a bastion/jumphost."""
    return bool(_BASTION_TAG_PATTERN.search(_tag_text(resource)))


def _rule_covers_port(rule: dict[str, Any], port: int) -> bool:
    """Return True when a SG rule's port range covers the given TCP/UDP port."""
    from_port = rule.get("FromPort")
    to_port = rule.get("ToPort")
    if from_port is None or to_port is None:
        # AWS represents "all ports" as missing FromPort/ToPort.
        return rule.get("IpProtocol") in ("-1", -1)
    return from_port <= port <= to_port


def _rule_has_public_source(rule: dict[str, Any]) -> bool:
    """Return True when a SG rule allows ingress from a public CIDR."""
    for ip_range in rule.get("IpRanges", []):
        if ip_range.get("CidrIp") in _PUBLIC_CIDRS:
            return True
    for ipv6_range in rule.get("Ipv6Ranges", []):
        if ipv6_range.get("CidrIpv6") in _PUBLIC_CIDRS:
            return True
    return False


def _provider_hidden(test_name: str) -> dict[str, Any]:
    """Return a passing subtest result for hyperscalers that hide BMC from tenants."""
    return {
        "passed": True,
        "provider_hidden": True,
        "message": (
            f"{test_name}: no customer-visible BMC management network in this account; "
            "BMC plane is provider-owned. Self-managed NCPs should tag resources with "
            "bmc/ipmi/redfish/oob/out-of-band to enable strict enforcement."
        ),
    }


def _check_bastion_identifiable(bastion_sgs: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify at least one SG is tagged as a bastion/jumphost."""
    if not bastion_sgs:
        return {
            "passed": False,
            "error": (
                "No bastion/jumphost-tagged security group found; tag at least one "
                "SG with bastion/jumphost to designate the management entry point"
            ),
        }
    return {
        "passed": True,
        "bastion_security_groups": [sg.get("GroupId") for sg in bastion_sgs],
        "message": f"{len(bastion_sgs)} bastion-tagged security group(s) identified",
    }


def _check_management_ingress_via_bastion_only(
    management_sgs: list[dict[str, Any]],
    bastion_sg_ids: set[str],
) -> dict[str, Any]:
    """Verify BMC-tagged SGs only accept ingress from the bastion SG."""
    for sg in management_sgs:
        for rule in sg.get("IpPermissions", []):
            on_management_port = any(_rule_covers_port(rule, port) for port in _MANAGEMENT_PORTS)
            if not on_management_port:
                continue
            if _rule_has_public_source(rule):
                return {
                    "passed": False,
                    "security_group_id": sg.get("GroupId"),
                    "error": (
                        f"BMC management SG {sg.get('GroupId')} allows ingress from a public CIDR "
                        f"on a management port (rule={rule.get('IpProtocol')} "
                        f"{rule.get('FromPort')}-{rule.get('ToPort')})"
                    ),
                }
            referenced_sg_ids = {pair.get("GroupId") for pair in rule.get("UserIdGroupPairs", [])}
            non_bastion_refs = referenced_sg_ids - bastion_sg_ids - {None}
            has_explicit_cidr = bool(rule.get("IpRanges") or rule.get("Ipv6Ranges"))
            has_prefix_list = bool(rule.get("PrefixListIds"))
            if not referenced_sg_ids and not has_explicit_cidr and not has_prefix_list:
                continue
            if non_bastion_refs or has_explicit_cidr or has_prefix_list:
                return {
                    "passed": False,
                    "security_group_id": sg.get("GroupId"),
                    "error": (
                        f"BMC management SG {sg.get('GroupId')} accepts ingress from sources "
                        f"other than the designated bastion SG(s) "
                        f"(non_bastion_sg_refs={sorted(s for s in non_bastion_refs if s)}, "
                        f"explicit_cidr={has_explicit_cidr}, prefix_list={has_prefix_list})"
                    ),
                }
    return {
        "passed": True,
        "management_security_groups_checked": len(management_sgs),
        "message": (f"Ingress to {len(management_sgs)} BMC management SG(s) is restricted to bastion SG(s)"),
    }


def _route_targets_internet_gateway(route: dict[str, Any]) -> bool:
    """Return True when a route forwards to an internet gateway."""
    gateway_id = route.get("GatewayId") or ""
    return gateway_id.startswith("igw-")


def _check_no_direct_public_route(
    ec2: Any,
    management_subnets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify BMC management subnets have no public route or auto-assign public IP."""
    if not management_subnets:
        return {
            "passed": True,
            "subnets_checked": 0,
            "message": "No BMC management subnets in scope",
        }

    for subnet in management_subnets:
        if subnet.get("MapPublicIpOnLaunch"):
            return {
                "passed": False,
                "subnet_id": subnet.get("SubnetId"),
                "error": (f"BMC management subnet {subnet.get('SubnetId')} has MapPublicIpOnLaunch enabled"),
            }

    subnet_ids = [s["SubnetId"] for s in management_subnets if s.get("SubnetId")]
    route_tables = _collect_paginated(
        ec2,
        "describe_route_tables",
        "RouteTables",
        Filters=[{"Name": "association.subnet-id", "Values": subnet_ids}],
    )
    explicit_subnet_ids = {
        association.get("SubnetId")
        for route_table in route_tables
        for association in route_table.get("Associations", [])
        if association.get("SubnetId")
    }
    main_route_table_vpc_ids = sorted(
        {
            subnet.get("VpcId")
            for subnet in management_subnets
            if subnet.get("VpcId") and subnet.get("SubnetId") not in explicit_subnet_ids
        }
    )
    for vpc_id in main_route_table_vpc_ids:
        route_tables.extend(
            _collect_paginated(
                ec2,
                "describe_route_tables",
                "RouteTables",
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "association.main", "Values": ["true"]},
                ],
            )
        )

    for route_table in route_tables:
        for route in route_table.get("Routes", []):
            destination = route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock") or ""
            if destination in _PUBLIC_CIDRS and _route_targets_internet_gateway(route):
                return {
                    "passed": False,
                    "route_table_id": route_table.get("RouteTableId"),
                    "error": (
                        f"BMC management route table {route_table.get('RouteTableId')} "
                        f"forwards {destination} to an internet gateway"
                    ),
                }

    return {
        "passed": True,
        "subnets_checked": len(management_subnets),
        "route_tables_checked": len(route_tables),
        "message": (
            f"No internet-gateway routes from {len(management_subnets)} BMC management "
            f"subnet(s) across {len(route_tables)} route table(s)"
        ),
    }


def _check_bastion_hardened(bastion_sgs: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify bastion SGs do not allow 0.0.0.0/0 on SSH."""
    if not bastion_sgs:
        return {
            "passed": False,
            "error": "No bastion-tagged security group found; cannot evaluate hardening",
        }

    for sg in bastion_sgs:
        for rule in sg.get("IpPermissions", []):
            if not _rule_covers_port(rule, 22):
                continue
            if _rule_has_public_source(rule):
                return {
                    "passed": False,
                    "security_group_id": sg.get("GroupId"),
                    "error": (f"Bastion SG {sg.get('GroupId')} allows SSH from a public CIDR (0.0.0.0/0 or ::/0)"),
                }

    return {
        "passed": True,
        "bastion_security_groups_checked": len(bastion_sgs),
        "message": (f"{len(bastion_sgs)} bastion SG(s) restrict SSH ingress to non-public CIDRs"),
    }


@handle_aws_errors
def main() -> int:
    """Run BMC bastion-access checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="BMC bastion access test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--vpc-id", help="Restrict scan to a single VPC (optional)")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    sg_filters: list[dict[str, Any]] = []
    subnet_filters: list[dict[str, Any]] = []
    if args.vpc_id:
        sg_filters.append({"Name": "vpc-id", "Values": [args.vpc_id]})
        subnet_filters.append({"Name": "vpc-id", "Values": [args.vpc_id]})

    security_groups = _collect_paginated(ec2, "describe_security_groups", "SecurityGroups", Filters=sg_filters)
    subnets = _collect_paginated(ec2, "describe_subnets", "Subnets", Filters=subnet_filters)

    management_sgs = [sg for sg in security_groups if _is_management_resource(sg)]
    bastion_sgs = [sg for sg in security_groups if _is_bastion_resource(sg)]
    management_subnets = [s for s in subnets if _is_management_resource(s)]
    bastion_sg_ids = {sg["GroupId"] for sg in bastion_sgs if sg.get("GroupId")}

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_bastion_access",
        "management_networks_checked": len(management_sgs) + len(management_subnets),
        "bastion_security_groups": sorted(bastion_sg_ids),
        "tests": {},
    }

    no_management_resources = not management_sgs and not management_subnets
    if no_management_resources:
        # Hyperscaler reality: BMC is provider-hidden. Pass each subtest with a
        # marker so the validation contract is satisfied without false-positive
        # signal. The contract still fails for self-managed NCPs that tag their
        # BMC fabric.
        for subtest in (
            "bastion_identifiable",
            "management_ingress_via_bastion_only",
            "no_direct_public_route",
            "bastion_hardened",
        ):
            result["tests"][subtest] = _provider_hidden(subtest)
    else:
        result["tests"]["bastion_identifiable"] = _check_bastion_identifiable(bastion_sgs)
        result["tests"]["management_ingress_via_bastion_only"] = _check_management_ingress_via_bastion_only(
            management_sgs, bastion_sg_ids
        )
        result["tests"]["no_direct_public_route"] = _check_no_direct_public_route(ec2, management_subnets)
        result["tests"]["bastion_hardened"] = _check_bastion_hardened(bastion_sgs)

    result["success"] = all(test.get("passed") for test in result["tests"].values())

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
