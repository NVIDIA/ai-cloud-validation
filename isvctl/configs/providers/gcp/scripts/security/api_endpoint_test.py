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

"""Verify management API endpoints are not publicly exposed by default (GCP).

Reads each GKE cluster's private-cluster posture via Cloud Container
ClusterManagerClient.list_clusters / get_cluster and confirms the control
plane is reachable only over private IP with a restricted authorized-network
allowlist. Where AWS inspects VPC interface-endpoint PrivateDnsEnabled and EKS
endpointPublicAccess, GKE exposes the equivalent posture as config-plane fields
on PrivateClusterConfig (enable_private_endpoint, public_endpoint) plus
master_authorized_networks_config.

Cluster evaluation is scoped to the test --region (regional and zonal clusters
within it), mirroring the AWS reference (EKS list_clusters is per-region) and
the suite's documented "regional GCP reads ... GKE" intent. A cluster in a
different region is outside the posture this run validates.

Subtests:

  1. probe_api_from_public:  every cluster control plane has
     enable_private_endpoint == True (the API is not reachable from a public
     management IP).
  2. probe_mgmt_from_public: every cluster has a non-empty
     master_authorized_networks_config (the management surface is restricted to
     an allowlist, and no entry is world-open 0.0.0.0/0).
  3. verify_private_only:    enable_private_endpoint == True AND the cluster
     exposes no public_endpoint.
  4. dns_not_public:         when an operator supplies a Cloud DNS private zone
     name, confirm it is PRIVATE-visibility; otherwise mark not-applicable and
     pass with a reason (GKE has no per-cluster private-DNS boolean).

When no clusters exist, the management endpoints are vacuously not-public, so
each control-plane subtest passes with a note (mirrors the AWS no-EKS sub-test
pass). A public-only control plane or a world-open authorized CIDR is a hard
fail.

Usage:
    python3 api_endpoint_test.py --region us-central1 --project my-project

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "api_endpoint_isolation",
    "endpoints_tested": <count>,
    "tests": {
        "probe_api_from_public": {"passed": true, ...},
        "probe_mgmt_from_public": {"passed": true, ...},
        "verify_private_only": {"passed": true, ...},
        "dns_not_public": {"passed": true, ...}
    }
  }
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.cloud import container_v1


def _is_world_open_cidr(cidr: str) -> bool:
    """Return True when a CIDR covers the entire IPv4 or IPv6 internet."""
    try:
        return ipaddress.ip_network(cidr, strict=False).prefixlen == 0
    except ValueError:
        return False


def _in_region(location: str, region: str) -> bool:
    """Return True when a cluster ``location`` is the test ``region``.

    GKE encodes a cluster's location as either the region itself (regional
    clusters, e.g. ``us-central1``) or a zone within it (zonal clusters, e.g.
    ``us-central1-a``). A blank region disables scoping (evaluate everything).
    """
    if not region:
        return True
    return location == region or location.startswith(f"{region}-")


def _list_clusters(client: container_v1.ClusterManagerClient, project: str, region: str) -> list[container_v1.Cluster]:
    """List GKE clusters in ``region`` under ``project``.

    A single ``-`` location call enumerates every cluster (regional and zonal),
    then the result is scoped to the test region's regional + zonal clusters.
    This mirrors the AWS reference, which lists EKS clusters per region
    (``boto3.client("eks", region_name=region)``), and the documented intent of
    the suite ``region`` setting ("regional GCP reads ... GKE"): a cluster in a
    different region is outside the posture this run validates.
    """
    parent = f"projects/{project}/locations/-"
    response = client.list_clusters(parent=parent)
    return [c for c in response.clusters if _in_region(c.location, region)]


def _evaluate_clusters(clusters: list[container_v1.Cluster]) -> dict[str, dict[str, Any]]:
    """Evaluate the control-plane subtests over the cluster inventory.

    Returns a dict keyed by subtest name. When no clusters exist the management
    control plane is vacuously not-public, so each subtest passes with a note.
    """
    if not clusters:
        note = "No GKE clusters in region (control plane endpoints vacuously not public)"
        return {
            "probe_api_from_public": {"passed": True, "message": note},
            "probe_mgmt_from_public": {"passed": True, "message": note},
            "verify_private_only": {"passed": True, "message": note},
        }

    public_api: list[str] = []  # control plane reachable from public IP
    unrestricted_mgmt: list[str] = []  # no authorized-networks allowlist
    world_open_mgmt: list[str] = []  # authorized network includes 0.0.0.0/0
    public_endpoint_present: list[str] = []  # cluster advertises a public endpoint

    for cluster in clusters:
        private_cfg = cluster.private_cluster_config
        if not private_cfg or not private_cfg.enable_private_endpoint:
            public_api.append(cluster.name)
        if private_cfg and private_cfg.public_endpoint:
            public_endpoint_present.append(cluster.name)

        authorized = cluster.master_authorized_networks_config
        cidr_blocks = list(authorized.cidr_blocks) if authorized else []
        if not authorized or not cidr_blocks:
            unrestricted_mgmt.append(cluster.name)
        elif any(_is_world_open_cidr(block.cidr_block) for block in cidr_blocks):
            world_open_mgmt.append(cluster.name)

    total = len(clusters)
    tests: dict[str, dict[str, Any]] = {}

    if public_api:
        tests["probe_api_from_public"] = {
            "passed": False,
            "error": f"Clusters with public control-plane endpoint: {public_api}",
        }
    else:
        tests["probe_api_from_public"] = {
            "passed": True,
            "message": f"{total} cluster control plane(s) are private-endpoint only",
        }

    if unrestricted_mgmt:
        tests["probe_mgmt_from_public"] = {
            "passed": False,
            "error": f"Clusters without an authorized-networks allowlist: {unrestricted_mgmt}",
        }
    elif world_open_mgmt:
        tests["probe_mgmt_from_public"] = {
            "passed": False,
            "error": f"Clusters with a world-open (0.0.0.0/0) authorized network: {world_open_mgmt}",
        }
    else:
        tests["probe_mgmt_from_public"] = {
            "passed": True,
            "message": f"{total} cluster(s) restrict management access to an allowlist",
        }

    if public_api or public_endpoint_present:
        offenders = sorted(set(public_api) | set(public_endpoint_present))
        tests["verify_private_only"] = {
            "passed": False,
            "error": f"Clusters not private-only (public endpoint or public access): {offenders}",
        }
    else:
        tests["verify_private_only"] = {
            "passed": True,
            "message": f"{total} cluster(s) expose no public control-plane endpoint",
        }

    return tests


def _check_private_dns(client: Any, project: str, zone_name: str) -> dict[str, Any]:
    """Confirm an operator-supplied Cloud DNS managed zone is PRIVATE-visibility.

    GKE has no per-cluster private-DNS boolean, so DNS isolation is only
    verifiable when an operator names a managed zone. With no zone supplied this
    sub-signal is not applicable and passes with a reason.
    """
    if not zone_name:
        return {
            "passed": True,
            "message": "No Cloud DNS zone supplied (DNS visibility check not applicable)",
        }
    zone = client.managed_zones().get(project=project, managedZone=zone_name).execute()
    visibility = str(zone.get("visibility", "")).lower()
    if visibility == "private":
        return {"passed": True, "message": f"Managed zone {zone_name!r} is private-visibility"}
    return {
        "passed": False,
        "error": f"Managed zone {zone_name!r} visibility is {visibility or 'public'!r}, not private",
    }


@handle_gcp_errors
def main() -> int:
    """Run API endpoint isolation checks over GKE clusters and emit JSON result."""
    parser = argparse.ArgumentParser(description="GCP API endpoint isolation test")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument(
        "--dns-zone",
        default="",
        help="Optional Cloud DNS managed-zone name to confirm PRIVATE visibility",
    )
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "api_endpoint_isolation",
        "endpoints_tested": 0,
        "tests": {
            "probe_api_from_public": {"passed": False},
            "probe_mgmt_from_public": {"passed": False},
            "verify_private_only": {"passed": False},
            "dns_not_public": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)
        client = container_v1.ClusterManagerClient()
        clusters = _list_clusters(client, project, args.region)
        result["endpoints_tested"] = len(clusters)

        control_plane_tests = _evaluate_clusters(clusters)
        for name, verdict in control_plane_tests.items():
            result["tests"][name] = verdict

        # dns_not_public is evaluated independently of cluster inventory: it is
        # only actionable when an operator names a private managed zone.
        if args.dns_zone:
            from googleapiclient import discovery

            dns_client = discovery.build("dns", "v1", cache_discovery=False)
            result["tests"]["dns_not_public"] = _check_private_dns(dns_client, project, args.dns_zone)
        else:
            result["tests"]["dns_not_public"] = _check_private_dns(None, project, "")

        result["success"] = all(t.get("passed") for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
