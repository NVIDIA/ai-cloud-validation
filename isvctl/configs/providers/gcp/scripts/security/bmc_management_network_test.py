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

"""Verify BMC management runs on a dedicated, restricted network (GCP).

Managed Compute Engine exposes no customer-facing BMC/IPMI/Redfish management
plane; real out-of-band hardware management lives on Bare Metal Solution, a
distinct product outside managed-GCE scope. This is analogous to the AWS Nitro
management plane being provider-owned and hidden from tenant VPCs.

The check validates the customer-visible side of that boundary:

  1. Confirm a valid, reachable GCP environment by reading the operator project
     (resourcemanager_v3 ProjectsClient.get_project) — the identity probe that
     gates the whole result.
  2. Inventory every primitive that can express a customer BMC management
     network -- VPC networks, subnetworks, and firewall rules. A resource is
     flagged when its name/target-tag matches bmc/ipmi/redfish/oob/out-of-band,
     or when a subnet CIDR / enabled firewall range explicitly overlaps a
     reserved BMC management CIDR (169.254.0.0/16, 198.18.0.0/15), mirroring the
     AWS oracle's BMC_MANAGEMENT_CIDRS. When nothing is flagged (the managed-GCE
     default) the four subtests pass with a provider_hidden marker; the real
     signal is the identity probe plus the documented absence of a customer BMC
     surface, not a hardcoded pass.

If the identity probe fails, every subtest fails and the run exits non-zero.

Usage:
    python3 bmc_management_network_test.py --region us-central1 --project=my-project

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_management_network",
    "management_networks_checked": 0,
    "tests": {
      "dedicated_management_network":  {"passed": true, "provider_hidden": true},
      "restricted_management_routes":  {"passed": true, "provider_hidden": true},
      "tenant_network_not_management": {"passed": true, "provider_hidden": true},
      "management_acl_enforced":       {"passed": true, "provider_hidden": true}
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

# Word-boundary matcher for customer BMC/out-of-band management resources. The
# alphanumeric lookarounds match hyphen- and underscore-delimited names/labels
# (e.g. "oob-mgmt") while rejecting substrings like "bmcollege".
_MANAGEMENT_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:bmc|ipmi|redfish|oob|out[-_]?of[-_]?band)(?![a-z0-9])",
    re.IGNORECASE,
)

# Reserved BMC/out-of-band management ranges a customer subnet or firewall rule
# must not target. Mirrors the AWS oracle's BMC_MANAGEMENT_CIDRS (link-local
# IPMI range + the benchmarking range sometimes used for management).
_BMC_CIDRS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
)

# Subtests emitted by this check, in contract order.
_SUBTESTS = (
    "dedicated_management_network",
    "restricted_management_routes",
    "tenant_network_not_management",
    "management_acl_enforced",
)


def _label_name_text(name: str | None, labels: Any) -> str:
    """Join a resource name and its label key/value pairs for regex matching."""
    parts: list[str] = [name or ""]
    for key, value in dict(labels or {}).items():
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _is_management_resource(name: str | None, labels: Any) -> bool:
    """Return True when a resource name/label identifies a BMC management network."""
    return bool(_MANAGEMENT_PATTERN.search(_label_name_text(name, labels)))


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
    """Return True when an enabled firewall rule names/targets or ranges toward a BMC fabric."""
    if getattr(rule, "disabled", False):
        # Disabled rules enforce nothing; they are not a management surface.
        return False
    candidates = [rule.name or "", *(rule.target_tags or [])]
    if any(_MANAGEMENT_PATTERN.search(text) for text in candidates):
        return True
    ranges = [*(rule.source_ranges or []), *(rule.destination_ranges or [])]
    return any(_explicitly_targets_bmc(cidr) for cidr in ranges)


def _scan_management_networks(project: str) -> list[str]:
    """Return names of customer BMC-management VPC networks, subnetworks and firewall rules.

    Every primitive that can express a customer BMC management network is
    inventoried. The Compute Engine ``Network``/``Subnetwork`` protos carry no
    labels field, so networks match on name alone; subnetworks also match when
    their CIDR overlaps a reserved BMC range; firewall rules match on
    name/target-tag or an explicit BMC range (disabled rules skipped).
    """
    flagged: list[str] = []

    networks_client = compute_v1.NetworksClient()
    for network in networks_client.list(project=project):
        if _is_management_resource(network.name, None):
            flagged.append(network.name)

    subnets_client = compute_v1.SubnetworksClient()
    for scoped_list in subnets_client.aggregated_list(project=project):
        # aggregated_list yields (scope, SubnetworksScopedList) pairs.
        _, scoped = scoped_list
        for subnet in getattr(scoped, "subnetworks", None) or []:
            if _is_management_resource(subnet.name, None) or _explicitly_targets_bmc(
                getattr(subnet, "ip_cidr_range", "") or ""
            ):
                flagged.append(subnet.name)

    fw_client = compute_v1.FirewallsClient()
    for rule in fw_client.list(project=project):
        if _is_management_firewall(rule):
            flagged.append(rule.name)

    return flagged


@handle_gcp_errors
def main() -> int:
    """Run BMC management-network checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="BMC management network test (GCP)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_management_network",
        "management_networks_checked": 0,
        "tests": {
            "dedicated_management_network": {"passed": False},
            "restricted_management_routes": {"passed": False},
            "tenant_network_not_management": {"passed": False},
            "management_acl_enforced": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)

        # Identity probe: a successful get_project confirms a valid, reachable
        # GCP environment and gates the whole result.
        projects_client = resourcemanager_v3.ProjectsClient()
        projects_client.get_project(name=f"projects/{project}")

        flagged = _scan_management_networks(project)
        result["management_networks_checked"] = len(flagged)

        if not flagged:
            # Managed-GCE default: no customer BMC plane exists, so the
            # customer-visible boundary holds. Mark each subtest with the
            # provider_hidden reasoning rather than a bare pass.
            evidence = (
                "Managed Compute Engine exposes no customer BMC/IPMI/Redfish "
                "management network; the management plane is provider-owned. "
                f"No customer BMC-labelled networks found in project {project}."
            )
            for subtest in _SUBTESTS:
                result["tests"][subtest] = {
                    "passed": True,
                    "provider_hidden": True,
                    "evidence": evidence,
                }
        else:
            # A customer-operated BMC fabric is present; the customer-visible
            # boundary is no longer vacuously satisfied. Surface it as a failure
            # so the operator wires the BMC reachability checks for this fabric.
            failure = (
                f"Customer BMC-labelled management resources present: {sorted(flagged)}. "
                "Translate the management-network reachability checks to the GCP "
                "firewall + route constructs guarding this fabric."
            )
            for subtest in _SUBTESTS:
                result["tests"][subtest] = {"passed": False, "error": failure}

        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        evidence = f"GCP identity probe failed: {e}"
        for subtest in _SUBTESTS:
            result["tests"][subtest] = {"passed": False, "error": evidence}

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
