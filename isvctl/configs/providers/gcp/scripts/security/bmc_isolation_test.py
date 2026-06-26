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

"""Verify BMC interfaces are not reachable from tenant networks (GCP).

Managed Compute Engine exposes no customer BMC/IPMI/Redfish management plane,
so tenant VPCs have no route toward such endpoints — analogous to the AWS
hypervisor management plane being unreachable from tenant VPCs. GCP has no
security groups or network ACLs; tenant reachability is governed by VPC routes
plus firewall rules.

The check validates the customer-visible isolation boundary by inventorying
every GCP primitive that can express tenant->BMC reachability -- VPC routes and
firewall rules (GCP has no security groups or NACLs):

  1. Confirm a valid GCP environment and enumerate VPC routes
     (compute_v1 RoutesClient.list) and firewall rules (FirewallsClient.list) --
     the identity/list probe that gates the whole result.
  2. Flag any route or firewall rule that references a customer BMC fabric,
     either by name/target-tag (bmc/ipmi/redfish/oob/out-of-band) or by a range
     that explicitly overlaps a reserved BMC/IPMI management CIDR
     (169.254.0.0/16, 198.18.0.0/15), mirroring the AWS oracle's BMC_CIDRS.
     Disabled firewall rules grant no reachability and are skipped; default
     routes (0.0.0.0/0) overlap every range and are not treated as BMC
     reachability. When nothing is flagged (the managed-GCE default) the four
     subtests pass with a provider_hidden marker; the real signal is the route +
     firewall enumeration plus the documented absence of a customer BMC surface,
     not a hardcoded pass.

If the route or firewall enumeration fails, every subtest fails and the run
exits non-zero.

Usage:
    python3 bmc_isolation_test.py --region us-central1 --project=my-project

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_tenant_isolation",
    "bmc_endpoints_tested": 0,
    "tests": {
      "probe_bmc_from_tenant": {"passed": true, "provider_hidden": true},
      "probe_ipmi_port":       {"passed": true, "provider_hidden": true},
      "probe_redfish_port":    {"passed": true, "provider_hidden": true},
      "reverse_path_check":    {"passed": true, "provider_hidden": true}
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
# alphanumeric lookarounds match hyphen- and underscore-delimited names while
# rejecting substrings like "bmcollege".
_MANAGEMENT_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:bmc|ipmi|redfish|oob|out[-_]?of[-_]?band)(?![a-z0-9])",
    re.IGNORECASE,
)

# Reserved BMC/out-of-band management ranges a tenant route or firewall rule
# must never explicitly target. Mirrors the AWS oracle's BMC_CIDRS (link-local
# IPMI range + the benchmarking range sometimes used for management).
_BMC_CIDRS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
)

# Subtests emitted by this check, in contract order.
_SUBTESTS = (
    "probe_bmc_from_tenant",
    "probe_ipmi_port",
    "probe_redfish_port",
    "reverse_path_check",
)


def _explicitly_targets_bmc(cidr: str | None) -> bool:
    """Return True when a CIDR explicitly overlaps a reserved BMC management range.

    Default/aggregate ranges (prefix length 0, e.g. 0.0.0.0/0) overlap every
    range and would mask the real signal, so they are not treated as BMC
    reachability -- only an explicit overlap with a reserved BMC range counts.
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


def _is_management_route(route: compute_v1.Route) -> bool:
    """Return True when a VPC route is named/targeted at, or routes toward, a BMC fabric."""
    candidates = [
        route.name or "",
        getattr(route, "next_hop_instance", "") or "",
        getattr(route, "next_hop_network", "") or "",
        getattr(route, "next_hop_vpn_tunnel", "") or "",
    ]
    if any(_MANAGEMENT_PATTERN.search(text) for text in candidates):
        return True
    return _explicitly_targets_bmc(getattr(route, "dest_range", "") or "")


def _is_management_firewall(rule: compute_v1.Firewall) -> bool:
    """Return True when an enabled firewall rule names/targets or ranges toward a BMC fabric."""
    if getattr(rule, "disabled", False):
        # Disabled rules grant no reachability; they are not an isolation breach.
        return False
    candidates = [rule.name or "", *(rule.target_tags or [])]
    if any(_MANAGEMENT_PATTERN.search(text) for text in candidates):
        return True
    ranges = [*(rule.source_ranges or []), *(rule.destination_ranges or [])]
    return any(_explicitly_targets_bmc(cidr) for cidr in ranges)


def _scan_bmc_routes(project: str) -> list[str]:
    """Return names of VPC routes that reference a customer BMC management fabric."""
    routes_client = compute_v1.RoutesClient()
    return [route.name for route in routes_client.list(project=project) if _is_management_route(route)]


def _scan_bmc_firewalls(project: str) -> list[str]:
    """Return names of firewall rules that reference a customer BMC management fabric."""
    fw_client = compute_v1.FirewallsClient()
    return [rule.name for rule in fw_client.list(project=project) if _is_management_firewall(rule)]


@handle_gcp_errors
def main() -> int:
    """Run BMC tenant-isolation checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="BMC tenant isolation test (GCP)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_tenant_isolation",
        "bmc_endpoints_tested": 0,
        "tests": {
            "probe_bmc_from_tenant": {"passed": False},
            "probe_ipmi_port": {"passed": False},
            "probe_redfish_port": {"passed": False},
            "reverse_path_check": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)

        # Identity probe: confirm a valid, reachable GCP environment.
        projects_client = resourcemanager_v3.ProjectsClient()
        projects_client.get_project(name=f"projects/{project}")

        # Inventory every primitive that can express tenant->BMC reachability
        # (routes + firewall rules) before deciding the boundary is vacuously
        # satisfied. A BMC path expressed through an unscanned surface must not
        # slip through as a provider_hidden pass.
        flagged = _scan_bmc_routes(project) + _scan_bmc_firewalls(project)

        if not flagged:
            # Managed-GCE default: no tenant route or firewall rule reaches a
            # customer BMC fabric because no such fabric exists. No live
            # endpoints are probed, so the informational endpoint count is 0.
            evidence = (
                "Managed Compute Engine exposes no customer BMC/IPMI/Redfish "
                "endpoints; tenant VPCs have no route or firewall rule toward a "
                f"management plane. No customer BMC routes or firewall rules found "
                f"in project {project}."
            )
            for subtest in _SUBTESTS:
                result["tests"][subtest] = {
                    "passed": True,
                    "provider_hidden": True,
                    "evidence": evidence,
                }
        else:
            failure = (
                f"Customer BMC routes/firewall rules present: {sorted(flagged)}. "
                "Translate the tenant-isolation reachability checks to the GCP "
                "routes + firewall egress guarding this fabric."
            )
            for subtest in _SUBTESTS:
                result["tests"][subtest] = {"passed": False, "error": failure}

        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        evidence = f"GCP route/firewall enumeration failed: {e}"
        for subtest in _SUBTESTS:
            result["tests"][subtest] = {"passed": False, "error": evidence}

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
