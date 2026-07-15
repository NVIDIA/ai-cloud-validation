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

The cluster and service-endpoint checks use separate inventories:

* GKE cluster configuration drives the three control-plane exposure subtests.
* Private Service Connect forwarding rules targeting ``all-apis`` or ``vpc-sc``
  drive ``dns_not_public``. That subtest accepts only endpoint-bound Cloud DNS
  evidence: either the automatic Service Directory private zone for the exact
  forwarding rule/VPC, or a private zone in that VPC with an A/AAAA record for
  the forwarding rule IP.

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
  4. dns_not_public:         every PSC endpoint for Google APIs has automatic or
     manual private DNS bound to the endpoint's exact VPC and IP/namespace.

When no clusters exist, the GKE subtests pass with a note (mirrors the AWS
no-EKS sub-test pass). When no matching PSC endpoint exists, ``dns_not_public``
passes as explicitly not applicable; it does not claim that private DNS was
exercised. A public-only control plane, world-open authorized CIDR, or PSC
endpoint without endpoint-bound private DNS is a hard fail.

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
from google.cloud import compute_v1, container_v1, dns

PSC_GOOGLE_API_TARGETS = {"all-apis", "vpc-sc"}


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


def _list_clusters(
    client: container_v1.ClusterManagerClient,
    project: str,
    region: str,
    cluster_name: str = "",
) -> list[container_v1.Cluster]:
    """List GKE clusters in ``region``, optionally selecting one exact fixture.

    A single ``-`` location call enumerates every cluster (regional and zonal),
    then the result is scoped to the test region's regional + zonal clusters.
    This mirrors the AWS reference, which lists EKS clusters per region
    (``boto3.client("eks", region_name=region)``), and the documented intent of
    the suite ``region`` setting ("regional GCP reads ... GKE"): a cluster in a
    different region is outside the posture this run validates. ``cluster_name``
    gives an operator an explicit fixture boundary without changing that region.
    """
    parent = f"projects/{project}/locations/-"
    response = client.list_clusters(parent=parent)
    clusters = [c for c in response.clusters if _in_region(c.location, region)]
    if cluster_name:
        clusters = [c for c in clusters if c.name == cluster_name]
        if not clusters:
            scope = region or "all locations"
            raise RuntimeError(f"requested GKE fixture {cluster_name!r} was not found in {scope}")
    return clusters


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read one field from either a REST dict or a GAPIC message."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _canonical_resource(value: Any) -> str:
    """Normalize absolute Google resource URLs to their ``projects/...`` path."""
    resource = str(value or "").rstrip("/")
    if "/projects/" in resource:
        return f"projects/{resource.split('/projects/', 1)[1]}"
    return resource.lstrip("/")


def _target_name(target: Any) -> str:
    """Return the terminal resource name from a forwarding-rule target."""
    return str(target or "").rstrip("/").rsplit("/", 1)[-1]


def _endpoint_ip(endpoint: Any) -> str:
    """Return a forwarding rule's address across GAPIC and replay objects."""
    return str(_field(endpoint, "I_p_address", "") or "")


def _endpoint_name(endpoint: Any) -> str:
    """Return a stable forwarding-rule identifier for diagnostics."""
    return str(_field(endpoint, "name", "") or _field(endpoint, "self_link", "") or "unnamed-endpoint")


def _list_psc_google_api_endpoints(
    client: compute_v1.GlobalForwardingRulesClient,
    project: str,
) -> list[compute_v1.ForwardingRule]:
    """List global PSC forwarding rules that target Google APIs."""
    return [rule for rule in client.list(project=project) if _target_name(rule.target) in PSC_GOOGLE_API_TARGETS]


def _list_rest_items(connection: Any, path: str, item_key: str) -> list[dict[str, Any]]:
    """Read all pages from one Cloud DNS REST collection."""
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query_params = {"pageToken": page_token} if page_token else None
        payload = connection.api_request(method="GET", path=path, query_params=query_params)
        items.extend(payload.get(item_key) or [])
        page_token = str(payload.get("nextPageToken") or "")
        if not page_token:
            return items


def _list_managed_zones(client: dns.Client, project: str) -> list[dict[str, Any]]:
    """List Cloud DNS managed zones as REST resources."""
    return _list_rest_items(client._connection, f"/projects/{project}/managedZones", "managedZones")


def _list_record_sets(client: dns.Client, project: str, zone_name: str) -> list[dict[str, Any]]:
    """List one managed zone's record sets as REST resources."""
    path = f"/projects/{project}/managedZones/{zone_name}/rrsets"
    return _list_rest_items(client._connection, path, "rrsets")


def _zone_networks(zone: dict[str, Any]) -> set[str]:
    """Return the canonical network resources bound to a private zone."""
    config = zone.get("privateVisibilityConfig") or {}
    networks = config.get("networks") or []
    return {_canonical_resource(network.get("networkUrl")) for network in networks if network.get("networkUrl")}


def _zone_visible_to_endpoint(zone: dict[str, Any], endpoint: Any) -> bool:
    """Return True when a private zone is visible to the endpoint's exact VPC."""
    if str(zone.get("visibility") or "").lower() != "private":
        return False
    endpoint_network = _canonical_resource(_field(endpoint, "network", ""))
    return bool(endpoint_network and endpoint_network in _zone_networks(zone))


def _registration_namespaces(endpoint: Any, project: str) -> set[str]:
    """Return Service Directory namespaces registered by a PSC endpoint."""
    registrations = _field(endpoint, "service_directory_registrations", []) or []
    namespaces: set[str] = set()
    for registration in registrations:
        namespace = str(_field(registration, "namespace", "") or "")
        if not namespace:
            continue
        if "/" in namespace:
            namespaces.add(_canonical_resource(namespace))
            continue
        region = _target_name(_field(registration, "service_directory_region", "")) or "us-central1"
        namespaces.add(f"projects/{project}/locations/{region}/namespaces/{namespace}")
    return namespaces


def _service_directory_namespace_url(zone: dict[str, Any]) -> str:
    """Return a managed zone's nested Service Directory namespace resource."""
    config = zone.get("serviceDirectoryConfig") or {}
    namespace = config.get("namespace") or {}
    return str(namespace.get("namespaceUrl") or "")


def _has_automatic_private_dns(endpoint: Any, zones: list[dict[str, Any]], project: str) -> bool:
    """Verify the endpoint's automatic Service Directory private DNS zone."""
    if bool(_field(endpoint, "no_automate_dns_zone", False)):
        return False
    namespaces = _registration_namespaces(endpoint, project)
    if not namespaces:
        return False
    for zone in zones:
        namespace_url = _service_directory_namespace_url(zone)
        if _zone_visible_to_endpoint(zone, endpoint) and _canonical_resource(namespace_url) in namespaces:
            return True
    return False


def _is_google_api_dns_name(name: str) -> bool:
    """Return True for documented PSC Google API DNS namespaces."""
    canonical = name.rstrip(".").lower()
    return any(
        canonical == suffix or canonical.endswith(f".{suffix}") for suffix in ("googleapis.com", "gcr.io", "gke.goog")
    )


def _record_points_to_endpoint(record: dict[str, Any], endpoint_ip: str) -> bool:
    """Return True when a Google API A/AAAA record contains the endpoint IP."""
    return (
        _is_google_api_dns_name(str(record.get("name") or ""))
        and str(record.get("type") or "").upper() in {"A", "AAAA"}
        and endpoint_ip in {str(value) for value in (record.get("rrdatas") or [])}
    )


def _has_manual_private_dns(
    endpoint: Any,
    zones: list[dict[str, Any]],
    records_by_zone: dict[str, list[dict[str, Any]]],
) -> bool:
    """Verify a VPC-bound private zone resolves to the forwarding-rule IP."""
    endpoint_ip = _endpoint_ip(endpoint)
    if not endpoint_ip:
        return False
    for zone in zones:
        zone_name = str(zone.get("name") or "")
        if not zone_name or not _zone_visible_to_endpoint(zone, endpoint):
            continue
        if any(_record_points_to_endpoint(record, endpoint_ip) for record in records_by_zone.get(zone_name, [])):
            return True
    return False


def _evaluate_psc_private_dns(
    endpoints: list[compute_v1.ForwardingRule],
    client: dns.Client | None,
    project: str,
) -> dict[str, Any]:
    """Evaluate endpoint-bound private DNS for Google APIs PSC endpoints."""
    if not endpoints:
        return {
            "passed": True,
            "message": "No PSC endpoint targeting all-apis/vpc-sc (dns_not_public not applicable)",
        }
    if client is None:
        return {"passed": False, "error": "Cloud DNS client unavailable for PSC endpoint verification"}

    zones = _list_managed_zones(client, project)
    records_by_zone: dict[str, list[dict[str, Any]]] = {}
    offenders: list[str] = []
    for endpoint in endpoints:
        if _has_automatic_private_dns(endpoint, zones, project):
            continue
        candidate_zone_names = {
            str(zone.get("name") or "")
            for zone in zones
            if zone.get("name") and _zone_visible_to_endpoint(zone, endpoint)
        }
        for zone_name in sorted(candidate_zone_names):
            if zone_name not in records_by_zone:
                records_by_zone[zone_name] = _list_record_sets(client, project, zone_name)
        if not _has_manual_private_dns(endpoint, zones, records_by_zone):
            offenders.append(_endpoint_name(endpoint))

    if offenders:
        return {
            "passed": False,
            "error": f"PSC Google API endpoints without endpoint-bound private DNS: {offenders}",
        }
    return {
        "passed": True,
        "message": f"{len(endpoints)} PSC Google API endpoint(s) have endpoint-bound private DNS",
    }


def _evaluate_clusters(clusters: list[container_v1.Cluster]) -> dict[str, dict[str, Any]]:
    """Evaluate the control-plane subtests over the cluster inventory.

    Returns a dict keyed by the three GKE subtest names. When no clusters exist
    the management control plane is vacuously not-public, so each passes with a
    note. ``dns_not_public`` is evaluated independently over PSC endpoints.
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
        public_access_enabled = not private_cfg or not private_cfg.enable_private_endpoint
        if public_access_enabled:
            public_api.append(cluster.name)
        if private_cfg and private_cfg.public_endpoint:
            public_endpoint_present.append(cluster.name)

        # Master authorized networks constrain the external control-plane
        # endpoint. A private-only cluster has no external endpoint to allowlist;
        # requiring an allowlist there would false-fail the stronger isolation.
        if public_access_enabled:
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
            "message": (
                f"{total} cluster(s) disable the external control-plane endpoint or restrict it to an allowlist"
            ),
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


@handle_gcp_errors
def main() -> int:
    """Run GKE exposure and PSC private-DNS checks, then emit JSON result."""
    parser = argparse.ArgumentParser(description="GCP API endpoint isolation test")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--cluster-name", default="", help="optional exact GKE fixture name")
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
        cluster_client = container_v1.ClusterManagerClient()
        clusters = _list_clusters(cluster_client, project, args.region, args.cluster_name)
        forwarding_rule_client = compute_v1.GlobalForwardingRulesClient()
        psc_endpoints = _list_psc_google_api_endpoints(forwarding_rule_client, project)
        dns_client = dns.Client(project=project) if psc_endpoints else None
        result["endpoints_tested"] = len(clusters) + len(psc_endpoints)

        control_plane_tests = _evaluate_clusters(clusters)
        for name, verdict in control_plane_tests.items():
            result["tests"][name] = verdict
        result["tests"]["dns_not_public"] = _evaluate_psc_private_dns(psc_endpoints, dns_client, project)

        result["success"] = all(t.get("passed") for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
