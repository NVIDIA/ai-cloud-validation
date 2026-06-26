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

"""Verify BMC is reachable only via a hardened bastion (GCP).

Managed Compute Engine exposes no customer BMC/IPMI/Redfish management plane, so
SEC12-03 cannot be fully validated against managed GCE — analogous to the AWS
Nitro management plane being provider-owned and hidden from tenant VPCs. GCP
uses firewall rules plus routes and instance network tags in place of the AWS
security-group/subnet/route-tag constructs.

The check validates the customer-visible side of the contract:

  1. Confirm a valid GCP environment and enumerate firewall rules
     (compute_v1 FirewallsClient.list) and subnetworks (SubnetworksClient
     .aggregated_list) -- the identity/list probe that gates the whole result.
  2. Inventory the firewall rules and subnetworks that can front a customer BMC
     management plane. A resource is flagged when its name/target-tag matches
     bmc/ipmi/redfish/oob/out-of-band/bastion/jumphost, or when a subnet CIDR /
     enabled firewall range explicitly overlaps a reserved BMC management CIDR
     (169.254.0.0/16, 198.18.0.0/15). When nothing is flagged (the managed-GCE
     default) the four subtests pass with a provider_hidden marker; the real
     signal is the firewall + subnet enumeration plus the documented absence of
     a customer BMC surface, not a hardcoded pass.

If the firewall or subnet enumeration fails, every subtest fails and the run
exits non-zero.

Usage:
    python3 bmc_bastion_access_test.py --region us-central1 --project=my-project

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_bastion_access",
    "management_networks_checked": 0,
    "tests": {
      "bastion_identifiable":                {"passed": true, "provider_hidden": true},
      "management_ingress_via_bastion_only": {"passed": true, "provider_hidden": true},
      "no_direct_public_route":              {"passed": true, "provider_hidden": true},
      "bastion_hardened":                    {"passed": true, "provider_hidden": true}
    }
  }
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.cloud import compute_v1, resourcemanager_v3

# Word-boundary matcher for customer BMC/out-of-band management plus bastion
# resources. The alphanumeric lookarounds match hyphen- and underscore-delimited
# names/tags (e.g. "oob-mgmt", "jump-host") while rejecting substrings like
# "bmcollege".
_MANAGEMENT_PATTERN = re.compile(
    r"(?<![a-z0-9])"
    r"(?:bmc|ipmi|redfish|oob|out[-_]?of[-_]?band|bastion|jump[-_]?host|jumpbox)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)

# Reserved BMC/out-of-band management ranges a customer subnet or firewall rule
# must not front. Mirrors the AWS oracle's reserved BMC ranges (link-local IPMI
# range + the benchmarking range sometimes used for management).
_BMC_CIDRS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
)

# Subtests emitted by this check, in contract order.
_SUBTESTS = (
    "bastion_identifiable",
    "management_ingress_via_bastion_only",
    "no_direct_public_route",
    "bastion_hardened",
)


def _explicitly_targets_bmc(cidr: str | None) -> bool:
    """Return True when a CIDR explicitly overlaps a reserved BMC management range.

    Default/aggregate ranges (prefix length 0, e.g. 0.0.0.0/0) overlap every
    range and would mask the real signal, so they are not treated as BMC
    wiring -- only an explicit overlap with a reserved BMC range counts.
    """
    if not cidr:
        return False
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    if network.prefixlen == 0:
        return False
    return any(network.version == bmc.version and network.overlaps(bmc) for bmc in _BMC_CIDRS)


def _is_management_firewall(rule: compute_v1.Firewall) -> bool:
    """Return True when an enabled firewall rule names/targets or ranges toward a BMC/bastion."""
    if getattr(rule, "disabled", False):
        # Disabled rules front nothing; they are not a bastion/management surface.
        return False
    candidates = [rule.name or "", *(rule.target_tags or [])]
    if any(_MANAGEMENT_PATTERN.search(text) for text in candidates):
        return True
    ranges = [*(rule.source_ranges or []), *(rule.destination_ranges or [])]
    return any(_explicitly_targets_bmc(cidr) for cidr in ranges)


def _is_management_subnet(subnet: compute_v1.Subnetwork) -> bool:
    """Return True when a subnetwork names or ranges toward a customer BMC/bastion fabric."""
    if _MANAGEMENT_PATTERN.search(subnet.name or ""):
        return True
    return _explicitly_targets_bmc(getattr(subnet, "ip_cidr_range", "") or "")


def _scan_management_firewalls(project: str) -> list[str]:
    """Return names of firewall rules referencing a customer BMC/bastion fabric."""
    fw_client = compute_v1.FirewallsClient()
    return [rule.name for rule in fw_client.list(project=project) if _is_management_firewall(rule)]


def _scan_management_subnets(project: str) -> list[str]:
    """Return names of subnetworks fronting a customer BMC/bastion fabric."""
    flagged: list[str] = []
    subnets_client = compute_v1.SubnetworksClient()
    for scoped_list in subnets_client.aggregated_list(project=project):
        # aggregated_list yields (scope, SubnetworksScopedList) pairs.
        _, scoped = scoped_list
        for subnet in getattr(scoped, "subnetworks", None) or []:
            if _is_management_subnet(subnet):
                flagged.append(subnet.name)
    return flagged


@handle_gcp_errors
def main() -> int:
    """Run BMC bastion-access checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="BMC bastion access test (GCP)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_bastion_access",
        "management_networks_checked": 0,
        "tests": {
            "bastion_identifiable": {"passed": False},
            "management_ingress_via_bastion_only": {"passed": False},
            "no_direct_public_route": {"passed": False},
            "bastion_hardened": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)

        # Identity probe: confirm a valid, reachable GCP environment.
        projects_client = resourcemanager_v3.ProjectsClient()
        projects_client.get_project(name=f"projects/{project}")

        # Inventory both surfaces that can front a customer BMC plane (firewall
        # rules + subnetworks) before deciding the boundary is vacuously
        # satisfied. A bastion/BMC fabric expressed through an unscanned surface
        # must not slip through as a provider_hidden pass.
        flagged = _scan_management_firewalls(project) + _scan_management_subnets(project)
        result["management_networks_checked"] = len(flagged)

        if not flagged:
            # Managed-GCE default: no customer BMC plane and no bastion fronting
            # one, because no such fabric exists. The customer-visible boundary
            # holds; mark each subtest with the provider_hidden reasoning.
            evidence = (
                "Managed Compute Engine exposes no customer BMC management plane "
                "fronted by a bastion; the management plane is provider-owned. "
                f"No customer BMC/bastion firewall rules or subnetworks found in project {project}."
            )
            for subtest in _SUBTESTS:
                result["tests"][subtest] = {
                    "passed": True,
                    "provider_hidden": True,
                    "evidence": evidence,
                }
        else:
            failure = (
                f"Customer BMC/bastion firewall rules or subnetworks present: {sorted(flagged)}. "
                "Translate the bastion ingress / public-route / hardening checks to the "
                "GCP firewall + route constructs guarding this fabric."
            )
            for subtest in _SUBTESTS:
                result["tests"][subtest] = {"passed": False, "error": failure}

        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        evidence = f"GCP firewall/subnet enumeration failed: {e}"
        for subtest in _SUBTESTS:
            result["tests"][subtest] = {"passed": False, "error": evidence}

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
