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

"""Verify BMC management is on a dedicated, restricted network (AWS reference).

In AWS, the BMC/hypervisor management plane is provider-owned and is not
attached to customer VPCs. This reference check validates the customer-visible
side of that boundary:

  1. Tenant VPC CIDRs do not overlap reserved BMC/management ranges.
  2. Tenant route tables do not explicitly target management CIDRs.
  3. Tenant VPCs are not tagged as BMC/IPMI/Redfish management networks.
  4. Network ACLs do not explicitly allow management CIDRs.

Usage:
    python bmc_management_network_test.py --region us-west-2

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_management_network",
    "tests": {
      "dedicated_management_network": {"passed": true},
      "restricted_management_routes": {"passed": true},
      "tenant_network_not_management": {"passed": true},
      "management_acl_enforced": {"passed": true}
    }
  }
"""

import argparse
import ipaddress
import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from common.errors import handle_aws_errors

BMC_MANAGEMENT_CIDRS = [
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
]
_MANAGEMENT_TAG_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:bmc|ipmi|redfish|oob|out[-_]?of[-_]?band)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _collect_paginated(ec2: Any, operation_name: str, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Collect all items for a paginated EC2 describe operation."""
    paginator = ec2.get_paginator(operation_name)
    items: list[dict[str, Any]] = []
    for page in paginator.paginate(**kwargs):
        items.extend(page.get(result_key, []))
    return items


def _list_vpcs(ec2: Any, vpc_id: str | None) -> list[dict[str, Any]]:
    """Return the requested VPC, or every VPC in the region."""
    kwargs: dict[str, Any] = {"VpcIds": [vpc_id]} if vpc_id else {}
    return _collect_paginated(ec2, "describe_vpcs", "Vpcs", **kwargs)


def _iter_vpc_cidrs(vpc: dict[str, Any]) -> list[str]:
    """Return associated IPv4 CIDR blocks for a VPC."""
    cidrs: list[str] = []
    for association in vpc.get("CidrBlockAssociationSet", []):
        state = association.get("CidrBlockState", {}).get("State", "associated")
        cidr = association.get("CidrBlock")
        if cidr and state != "disassociated":
            cidrs.append(cidr)
    fallback_cidr = vpc.get("CidrBlock")
    if fallback_cidr and fallback_cidr not in cidrs:
        cidrs.append(fallback_cidr)
    return cidrs


def _cidr_overlaps_management(cidr: str) -> bool:
    """Return True when a CIDR overlaps a reserved BMC management range."""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return any(network.overlaps(management_cidr) for management_cidr in BMC_MANAGEMENT_CIDRS)


def _is_explicit_management_cidr(cidr: str | None) -> bool:
    """Return True when a CIDR explicitly targets a BMC management range.

    Default internet routes such as 0.0.0.0/0 are intentionally not treated
    as management routes here; this check catches explicit BMC network wiring.
    """
    if not cidr:
        return False
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    if network.prefixlen == 0:
        return False
    return any(
        network.version == management_cidr.version and network.overlaps(management_cidr)
        for management_cidr in BMC_MANAGEMENT_CIDRS
    )


def _check_dedicated_management_network(vpcs: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify selected tenant VPC CIDRs do not overlap management ranges."""
    cidrs_checked = 0
    for vpc in vpcs:
        for cidr in _iter_vpc_cidrs(vpc):
            cidrs_checked += 1
            if _cidr_overlaps_management(cidr):
                return {
                    "passed": False,
                    "vpc_id": vpc.get("VpcId"),
                    "error": f"Tenant VPC CIDR {cidr} overlaps reserved BMC management range",
                }

    return {
        "passed": True,
        "vpcs_tested": len(vpcs),
        "cidrs_checked": cidrs_checked,
        "message": f"No tenant VPC CIDRs overlap BMC management ranges across {len(vpcs)} VPCs",
    }


def _check_restricted_management_routes(ec2: Any, vpc_ids: list[str]) -> dict[str, Any]:
    """Verify tenant route tables have no explicit BMC management routes."""
    if not vpc_ids:
        return {"passed": True, "vpcs_tested": 0, "route_tables_checked": 0, "message": "No VPCs to scan"}

    route_tables = _collect_paginated(
        ec2,
        "describe_route_tables",
        "RouteTables",
        Filters=[{"Name": "vpc-id", "Values": vpc_ids}],
    )
    for route_table in route_tables:
        for route in route_table.get("Routes", []):
            destination = route.get("DestinationCidrBlock")
            if _is_explicit_management_cidr(destination):
                return {
                    "passed": False,
                    "vpc_id": route_table.get("VpcId"),
                    "route_tables_checked": len(route_tables),
                    "error": f"Route table {route_table['RouteTableId']} targets BMC management CIDR {destination}",
                }

    return {
        "passed": True,
        "vpcs_tested": len(vpc_ids),
        "route_tables_checked": len(route_tables),
        "message": f"No explicit BMC management routes across {len(route_tables)} route tables",
    }


def _has_management_tag(resource: dict[str, Any]) -> bool:
    """Return True when a resource tag identifies it as a BMC management network."""
    tag_text = " ".join(f"{tag.get('Key', '')}={tag.get('Value', '')}" for tag in resource.get("Tags", []))
    return bool(_MANAGEMENT_TAG_PATTERN.search(tag_text))


def _check_tenant_network_not_management(vpcs: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify selected tenant VPCs are not tagged as BMC management networks."""
    for vpc in vpcs:
        if _has_management_tag(vpc):
            return {
                "passed": False,
                "vpc_id": vpc.get("VpcId"),
                "error": f"Tenant VPC {vpc.get('VpcId')} is tagged as a BMC management network",
            }

    return {
        "passed": True,
        "vpcs_tested": len(vpcs),
        "message": f"{len(vpcs)} tenant VPCs are not labeled as BMC management networks",
    }


def _check_management_acl_enforced(ec2: Any, vpc_ids: list[str]) -> dict[str, Any]:
    """Verify network ACLs do not explicitly allow BMC management CIDRs."""
    if not vpc_ids:
        return {"passed": True, "vpcs_tested": 0, "network_acls_checked": 0, "message": "No VPCs to scan"}

    network_acls = _collect_paginated(
        ec2,
        "describe_network_acls",
        "NetworkAcls",
        Filters=[{"Name": "vpc-id", "Values": vpc_ids}],
    )
    for acl in network_acls:
        for entry in acl.get("Entries", []):
            cidr = entry.get("CidrBlock")
            if entry.get("RuleAction") == "allow" and _is_explicit_management_cidr(cidr):
                return {
                    "passed": False,
                    "vpc_id": acl.get("VpcId"),
                    "network_acls_checked": len(network_acls),
                    "error": f"Network ACL {acl['NetworkAclId']} explicitly allows BMC management CIDR {cidr}",
                }

    return {
        "passed": True,
        "vpcs_tested": len(vpc_ids),
        "network_acls_checked": len(network_acls),
        "message": f"No explicit BMC management ACL allows across {len(network_acls)} network ACLs",
    }


@handle_aws_errors
def main() -> int:
    """Run BMC management-network checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="BMC management network test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--vpc-id", help="Existing VPC to test (optional; scans all VPCs when omitted)")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)

    vpcs = _list_vpcs(ec2, args.vpc_id)
    vpc_ids = [vpc["VpcId"] for vpc in vpcs if vpc.get("VpcId")]

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_management_network",
        "management_networks_checked": len(vpc_ids),
        "vpcs_tested": len(vpc_ids),
        "vpc_ids_tested": vpc_ids,
        "tests": {},
    }

    if not vpc_ids:
        result["tests"] = {
            "dedicated_management_network": {
                "passed": False,
                "error": "No VPCs found to validate; provide --vpc-id",
            },
            "restricted_management_routes": {"passed": False, "error": "Validation not executed"},
            "tenant_network_not_management": {"passed": False, "error": "Validation not executed"},
            "management_acl_enforced": {"passed": False, "error": "Validation not executed"},
        }
    else:
        result["tests"]["dedicated_management_network"] = _check_dedicated_management_network(vpcs)
        result["tests"]["restricted_management_routes"] = _check_restricted_management_routes(ec2, vpc_ids)
        result["tests"]["tenant_network_not_management"] = _check_tenant_network_not_management(vpcs)
        result["tests"]["management_acl_enforced"] = _check_management_acl_enforced(ec2, vpc_ids)

    result["success"] = all(test.get("passed") for test in result["tests"].values())

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
