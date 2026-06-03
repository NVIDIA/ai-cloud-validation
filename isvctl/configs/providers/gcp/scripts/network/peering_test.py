#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""VPC peering test for Compute Engine (test phase, step ``peering_test``).

Translates the AWS provider's ``peering_test`` workflow to Compute Engine. The
six named subtests VpcPeeringCheck requires (``create_vpc_a``,
``create_vpc_b``, ``create_peering``, ``accept_peering``, ``add_routes``,
``peering_active``) are preserved by JSON key.

Documented divergences:

  * Peering has NO create + accept handshake. Compute Engine peering is
    bilateral and SYMMETRIC: BOTH sides call ``NetworksClient.add_peering``
    with the same peering name. ``create_peering`` adds the A->B leg;
    ``accept_peering`` adds the symmetric B->A leg (there is no separate
    accept API) and then confirms the peering via ``network_peerings``.
  * Networks own NO CIDR — ``--cidr-a`` / ``--cidr-b`` are applied to a
    subnetwork on each network so there are real subnet routes to exchange.
  * ``peering_active`` polls ``network_peerings`` on each side until a
    peering reaches ``state == "ACTIVE"``.
  * ``add_routes`` relies on AUTO subnet-route exchange (no manual route
    creation on Compute Engine). A peering reaching ACTIVE is necessary but
    NOT sufficient — auto-exchanged routes lag the ACTIVE state by a 30-90s
    window. ``add_routes.passed`` is gated on OBSERVING >=1 exchanged route
    via ``list_peering_routes`` (polled on a 5s interval up to ~120s), NOT
    on the peering state alone. ``list_peering_routes`` REQUIRES
    ``region``, ``direction`` AND ``peering_name`` keywords — the API
    rejects the call with "Required field 'peeringName' not specified" if
    the peering name is omitted.

The test creates and deletes its OWN two networks. The ``finally`` block
calls ``remove_peering`` on BOTH sides, then deletes the subnetworks, then
the networks, gated on the per-resource created flags.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    add_peering,
    delete_network,
    delete_subnetwork,
    insert_network,
    insert_subnetwork,
    list_peering_routes,
    network_peerings,
    remove_peering,
)

# Auto-exchanged routes lag the ACTIVE peering state; poll within this budget.
ROUTE_POLL_TIMEOUT = 120
ROUTE_POLL_INTERVAL = 5
PEERING_ACTIVE_TIMEOUT = 120
PEERING_ACTIVE_INTERVAL = 5


def _wait_peering_active(project: str, network_name: str, *, timeout: int, interval: int) -> bool:
    """Poll ``network_peerings`` until any peering reports ``state == "ACTIVE"``."""
    deadline = time.monotonic() + timeout
    while True:
        for p in network_peerings(project, network_name):
            if str(getattr(p, "state", "") or "").upper() == "ACTIVE":
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _count_exchanged_routes(project: str, network_name: str, region: str, peering_name: str) -> int:
    """Count INCOMING exchanged peering routes received from the peer.

    ``list_peering_routes`` REQUIRES ``region``, ``direction`` AND
    ``peering_name`` (the API rejects the call otherwise — omitting
    ``direction`` raises "Required field 'direction' not specified" and
    omitting the peering name raises "Required field 'peeringName' not
    specified"). INCOMING returns routes received from the named peer.

    Each returned object is a ``compute_v1.ExchangedPeeringRoute`` whose
    field set is ``dest_range``, ``imported``, ``next_hop_region``,
    ``priority``, ``type_`` — there is NO ``state`` field on this proto (the
    ``state``/``ACTIVE`` enum lives on ``NetworkPeering``, a different
    type). We therefore count routes that carry a real destination range:
    every auto-exchanged subnet route has a ``dest_range``, so a non-empty
    ``dest_range`` is the honest "this route was exchanged" signal. Gating
    on a nonexistent ``state`` field would be a deterministic false-negative.
    """
    routes = list_peering_routes(project, network_name, region=region, direction="INCOMING", peering_name=peering_name)
    return sum(1 for r in routes if getattr(r, "dest_range", ""))


def _poll_exchanged_routes(
    project: str, network_name: str, region: str, peering_name: str, *, timeout: int, interval: int
) -> int:
    """Poll ``list_peering_routes`` until >=1 exchanged route, return the count."""
    deadline = time.monotonic() + timeout
    while True:
        count = _count_exchanged_routes(project, network_name, region, peering_name)
        if count >= 1:
            return count
        if time.monotonic() >= deadline:
            return count
        time.sleep(interval)


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test VPC peering on Compute Engine")
    parser.add_argument("--region", required=True, help="GCP region for subnetworks / peering routes")
    parser.add_argument("--cidr-a", default="10.88.0.0/24", help="Subnet CIDR for VPC A")
    parser.add_argument("--cidr-b", default="10.87.0.0/24", help="Subnet CIDR for VPC B")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    # Compute Engine names ARE the API IDs — run-id-suffix everything.
    network_a = unique_suffix("isv-peering-a")
    network_b = unique_suffix("isv-peering-b")
    subnet_a = unique_suffix("isv-peering-a-subnet")
    subnet_b = unique_suffix("isv-peering-b-subnet")
    # Symmetric peering name used on BOTH sides.
    peering_name = unique_suffix("isv-peering")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "peering_test",
        "tests": {
            "create_vpc_a": {"passed": False},
            "create_vpc_b": {"passed": False},
            "create_peering": {"passed": False},
            "accept_peering": {"passed": False},
            "add_routes": {"passed": False},
            "peering_active": {"passed": False},
        },
        "vpc_a": {"id": network_a, "cidr": args.cidr_a},
        "vpc_b": {"id": network_b, "cidr": args.cidr_b},
    }

    # Cleanup trackers for the finally block.
    network_a_created = False
    network_b_created = False
    subnet_a_created = False
    subnet_b_created = False
    peering_a_added = False
    peering_b_added = False

    try:
        # 1. create_vpc_a — network A + a subnet carved from cidr-a.
        # Stamp each *_created tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. The finally
        # cleanup gates on the tracker, so it must be True before the call for a
        # partial create to still reach cleanup (delete on a never-created
        # resource is a harmless NotFound no-op). Mirrors create_vpc/byoip_test.
        network_a_created = True
        insert_network(project, network_a)
        subnet_a_created = True
        insert_subnetwork(project, args.region, subnet_a, network_a, args.cidr_a)
        result["tests"]["create_vpc_a"] = {"passed": True, "vpc_id": network_a}

        # 2. create_vpc_b — network B + a subnet carved from cidr-b.
        network_b_created = True
        insert_network(project, network_b)
        subnet_b_created = True
        insert_subnetwork(project, args.region, subnet_b, network_b, args.cidr_b)
        result["tests"]["create_vpc_b"] = {"passed": True, "vpc_id": network_b}

        # 3. create_peering — add the A->B leg (name set ONLY on the
        # network_peering; never on the top-level request — helper enforces).
        # Stamp the cleanup tracker BEFORE add_peering: the helper submits the
        # add op then waits for it (common/network.py add_peering). If the op
        # is ACCEPTED but the wait raises, the peering can still be created;
        # the finally block gates remove_peering on this flag, so it must be
        # True before the call or a leaked peering would block VPC deletion.
        # remove_peering is idempotent on already-absent, so stamping before a
        # never-submitted add is a harmless no-op (mirrors the network/subnet
        # stamp-before-insert discipline elsewhere in this provider).
        peering_a_added = True
        add_peering(project, network_a, network_b, peering_name)
        result["tests"]["create_peering"] = {"passed": True, "peering_id": peering_name}

        # 4. accept_peering — peering is symmetric: add the B->A leg with the
        # same peering name (no separate accept call). Confirm via read-back
        # that the peering reached ACTIVE on either side. Stamp the B->A
        # cleanup tracker before its add for the same accepted-but-wait-raised
        # reason as the A->B leg above.
        peering_b_added = True
        add_peering(project, network_b, network_a, peering_name)
        active_after_both = _wait_peering_active(
            project, network_a, timeout=PEERING_ACTIVE_TIMEOUT, interval=PEERING_ACTIVE_INTERVAL
        )
        result["tests"]["accept_peering"] = {
            "passed": active_after_both,
            "status": "ACTIVE" if active_after_both else "INACTIVE",
        }

        # 5. peering_active — confirm ACTIVE on BOTH sides before gating routes.
        active_a = active_after_both
        active_b = _wait_peering_active(
            project, network_b, timeout=PEERING_ACTIVE_TIMEOUT, interval=PEERING_ACTIVE_INTERVAL
        )
        result["tests"]["peering_active"] = {
            "passed": active_a and active_b,
            "status": "ACTIVE" if (active_a and active_b) else "INACTIVE",
            "requester_cidr": args.cidr_a,
            "accepter_cidr": args.cidr_b,
        }

        # 6. add_routes — rely on auto subnet-route exchange. Peering is
        # bilateral/symmetric, so BOTH directions must propagate. Only after
        # the peering is ACTIVE on both sides, poll list_peering_routes
        # (region + direction) on EACH side and gate on observing >=1
        # exchanged route on BOTH: a peering whose B->A leg silently failed
        # while A->B succeeded is an honest false-pass if we gate on A alone.
        # Both sides are polled (not just A) so the gate does not flake on
        # one-sided propagation lag.
        vpc_a_routes = 0
        vpc_b_routes = 0
        if active_a and active_b:
            vpc_a_routes = _poll_exchanged_routes(
                project, network_a, args.region, peering_name, timeout=ROUTE_POLL_TIMEOUT, interval=ROUTE_POLL_INTERVAL
            )
            vpc_b_routes = _poll_exchanged_routes(
                project, network_b, args.region, peering_name, timeout=ROUTE_POLL_TIMEOUT, interval=ROUTE_POLL_INTERVAL
            )
        result["tests"]["add_routes"] = {
            "passed": vpc_a_routes >= 1 and vpc_b_routes >= 1,
            "vpc_a_routes": vpc_a_routes,
            "vpc_b_routes": vpc_b_routes,
            "message": "peering subnet routes auto-exchanged in both directions",
        }

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Remove the peering on BOTH sides (idempotent on already-absent),
        # then delete subnetworks, then networks. Gate on created flags.
        # remove_peering has no retry wrapper and CAN raise, so wrap each call
        # individually — one failed peering removal must not skip the later
        # subnet/network deletes. delete_with_retry never raises and returns
        # False only on exhausted retries; capture every bool so a leaked
        # resource fails the step instead of coexisting with success=True.
        cleanup_errors: list[str] = []
        if peering_a_added:
            print(f"Cleanup: removing peering {peering_name} from {network_a}", file=sys.stderr)
            try:
                remove_peering(project, network_a, peering_name)
            except Exception as cleanup_exc:
                print(f"Cleanup error: {cleanup_exc}", file=sys.stderr)
                cleanup_errors.append(f"peering {peering_name} on {network_a}")
        if peering_b_added:
            print(f"Cleanup: removing peering {peering_name} from {network_b}", file=sys.stderr)
            try:
                remove_peering(project, network_b, peering_name)
            except Exception as cleanup_exc:
                print(f"Cleanup error: {cleanup_exc}", file=sys.stderr)
                cleanup_errors.append(f"peering {peering_name} on {network_b}")
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
