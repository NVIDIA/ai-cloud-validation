#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""VPC isolation test for Compute Engine (step ``vpc_isolation``).

Translates the AWS provider's two-VPC isolation workflow to Compute Engine.
The seven named subtests VpcIsolationCheck requires (``create_vpc_a``,
``create_vpc_b``, ``no_peering``, ``no_cross_routes_a``,
``no_cross_routes_b``, ``sg_isolation_a``, ``sg_isolation_b``) are
preserved by JSON key.

Documented divergences:

  * Networks own NO CIDR — ``--cidr-a`` / ``--cidr-b`` are aggregates from
    which a single subnetwork CIDR is carved per network. Each network is
    custom-mode (``auto_create_subnetworks=false``).
  * Custom-mode networks have NO default firewall rules — the default state
    is default-deny INGRESS. ``sg_isolation_a/b`` therefore map to: list
    firewall rules bound to the network and assert none has a source range
    overlapping the OTHER network's CIDR. An empty firewall list is the
    strongest possible default-deny and passes.
  * Peering is a property of a network (``Network.peerings``), not a
    separate resource. ``no_peering`` reads both networks' peerings lists
    and asserts neither references the other (or both are empty).
  * Routes are project-scoped, filtered to a network. ``no_cross_routes_a/b``
    list routes bound to the network and assert no ``Route.dest_range``
    overlaps the other network's CIDR.

The test creates and deletes BOTH networks (and their subnets). The
``finally`` block deletes subnets before networks (dependency order).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    carve_subnet_cidrs,
    delete_network,
    delete_subnetwork,
    insert_network,
    insert_subnetwork,
    is_auto_route,
    list_firewalls_for_network,
    list_routes_for_network,
    network_peerings,
)


def _cidrs_overlap(cidr_a: str, cidr_b: str) -> bool:
    """Return True iff the two CIDRs overlap (proper IP math, not prefix string)."""
    try:
        net_a = ipaddress.ip_network(cidr_a, strict=False)
        net_b = ipaddress.ip_network(cidr_b, strict=False)
    except ValueError:
        return False
    return net_a.overlaps(net_b)


def _check_no_peering(project: str, network_name: str, other_name: str) -> bool:
    """Return True iff ``network_name``'s peerings do not reference ``other_name``."""
    peerings = network_peerings(project, network_name)
    for p in peerings:
        # NetworkPeering.network is a self-link to the peered network.
        peer_link = str(getattr(p, "network", "") or "")
        if peer_link.rsplit("/", 1)[-1] == other_name:
            return False
    return True


def _check_no_cross_routes(project: str, network_name: str, other_cidr: str) -> dict[str, Any]:
    """Return a subtest dict: no route bound to the network reaches ``other_cidr``.

    GCE auto-created system routes are skipped: every custom-mode network is
    born with a default ``0.0.0.0/0`` internet-gateway route and a local
    subnet route. The internet route's ``0.0.0.0/0`` dest overlaps any CIDR,
    but it routes to the internet gateway — not to the other network — so it
    does not constitute cross-VPC reachability and is present on both
    isolated networks. Only operator/peering-created routes whose dest
    specifically overlaps the other network's range break isolation.
    """
    cross = []
    for route in list_routes_for_network(project, network_name):
        if is_auto_route(route):
            continue
        dest = str(getattr(route, "dest_range", "") or "")
        if dest and _cidrs_overlap(dest, other_cidr):
            cross.append({"route": route.name, "dest_range": dest})
    return {"passed": not cross, "cross_routes": cross}


def _check_sg_isolation(project: str, network_name: str, other_cidr: str) -> dict[str, Any]:
    """Return a subtest dict: no firewall on the network admits ``other_cidr``.

    The empty firewall list is the strongest default-deny on Compute Engine
    and passes. A firewall with a source range overlapping the other
    network's CIDR (or the catch-all 0.0.0.0/0) fails isolation.
    """
    offenders = []
    for fw in list_firewalls_for_network(project, network_name):
        for src in getattr(fw, "source_ranges", None) or []:
            if _cidrs_overlap(src, other_cidr):
                offenders.append({"firewall": fw.name, "source_range": src})
    return {"passed": not offenders, "offending_rules": offenders}


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test Compute Engine VPC isolation")
    parser.add_argument("--region", required=True, help="GCP region for the subnetworks")
    parser.add_argument("--cidr-a", default="10.97.0.0/16", help="Aggregate CIDR for VPC A")
    parser.add_argument("--cidr-b", default="10.96.0.0/16", help="Aggregate CIDR for VPC B")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    network_a = unique_suffix("isv-iso-a")
    network_b = unique_suffix("isv-iso-b")
    subnet_a = unique_suffix("isv-iso-a-subnet")
    subnet_b = unique_suffix("isv-iso-b-subnet")

    # Carve one /24 subnet CIDR from each aggregate. These are the ranges
    # the isolation checks reason about (the network itself has no CIDR).
    cidr_a = carve_subnet_cidrs(args.cidr_a, 1)[0]
    cidr_b = carve_subnet_cidrs(args.cidr_b, 1)[0]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "vpc_isolation",
        "tests": {
            "create_vpc_a": {"passed": False},
            "create_vpc_b": {"passed": False},
            "no_peering": {"passed": False},
            "no_cross_routes_a": {"passed": False},
            "no_cross_routes_b": {"passed": False},
            "sg_isolation_a": {"passed": False},
            "sg_isolation_b": {"passed": False},
        },
    }

    # Cleanup trackers.
    network_a_created = False
    network_b_created = False
    subnet_a_created = False
    subnet_b_created = False

    try:
        # 1 & 2. Create both custom-mode networks with one subnet each.
        # Stamp each *_created tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. The finally
        # cleanup gates on the tracker, so it must be True before the call for a
        # partial create to still reach cleanup (delete on a never-created
        # resource is a harmless NotFound no-op). Mirrors create_vpc/byoip_test.
        network_a_created = True
        insert_network(project, network_a)
        subnet_a_created = True
        insert_subnetwork(project, args.region, subnet_a, network_a, cidr_a)
        result["tests"]["create_vpc_a"] = {"passed": True, "vpc_id": network_a}

        network_b_created = True
        insert_network(project, network_b)
        subnet_b_created = True
        insert_subnetwork(project, args.region, subnet_b, network_b, cidr_b)
        result["tests"]["create_vpc_b"] = {"passed": True, "vpc_id": network_b}

        result["vpc_a"] = {"id": network_a, "cidr": cidr_a}
        result["vpc_b"] = {"id": network_b, "cidr": cidr_b}

        # 3. no_peering — neither network references the other (both empty
        # for freshly-created networks; assert it from a real read-back).
        no_peering = _check_no_peering(project, network_a, network_b) and _check_no_peering(
            project, network_b, network_a
        )
        result["tests"]["no_peering"] = {"passed": no_peering}

        # 4 & 5. no_cross_routes — no route on A reaches B's CIDR (and vice versa).
        result["tests"]["no_cross_routes_a"] = _check_no_cross_routes(project, network_a, cidr_b)
        result["tests"]["no_cross_routes_b"] = _check_no_cross_routes(project, network_b, cidr_a)

        # 6 & 7. sg_isolation — no firewall on A admits B's CIDR (and vice versa).
        result["tests"]["sg_isolation_a"] = _check_sg_isolation(project, network_a, cidr_b)
        result["tests"]["sg_isolation_b"] = _check_sg_isolation(project, network_b, cidr_a)

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Delete subnets before their networks (dependency order) on resources
        # THIS run created. delete_with_retry never raises and returns False
        # only on exhausted retries — capture every bool so a leaked resource
        # fails the step instead of coexisting with success=True. Each delete
        # is gated independently, so a failed sibling never skips the rest.
        cleanup_errors: list[str] = []
        if subnet_a_created:
            print(f"Cleanup: deleting subnetwork {subnet_a}", file=sys.stderr)
            if not delete_with_retry(
                delete_subnetwork, project, args.region, subnet_a, resource_desc=f"subnetwork {subnet_a}"
            ):
                cleanup_errors.append(f"subnetwork {subnet_a}")
        if subnet_b_created:
            print(f"Cleanup: deleting subnetwork {subnet_b}", file=sys.stderr)
            if not delete_with_retry(
                delete_subnetwork, project, args.region, subnet_b, resource_desc=f"subnetwork {subnet_b}"
            ):
                cleanup_errors.append(f"subnetwork {subnet_b}")
        if network_a_created:
            print(f"Cleanup: deleting network {network_a}", file=sys.stderr)
            if not delete_with_retry(delete_network, project, network_a, resource_desc=f"network {network_a}"):
                cleanup_errors.append(f"network {network_a}")
        if network_b_created:
            print(f"Cleanup: deleting network {network_b}", file=sys.stderr)
            if not delete_with_retry(delete_network, project, network_b, resource_desc=f"network {network_b}"):
                cleanup_errors.append(f"network {network_b}")
        if cleanup_errors:
            result.setdefault("cleanup_errors", []).extend(cleanup_errors)
            result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
