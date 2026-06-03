#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""VPC CRUD lifecycle test for Compute Engine (test phase, step ``vpc_crud``).

Translates the AWS provider's create/read/update/delete VPC workflow to
Compute Engine. The five named subtests VpcCrudCheck requires
(``create_vpc``, ``read_vpc``, ``update_tags``, ``update_dns``,
``delete_vpc``) are preserved by JSON key, but several map onto different
Compute Engine primitives because the EC2 analog does not exist.

Documented divergences:

  * Networks own NO CIDR — ``--cidr`` is the aggregate the test would carve
    subnetwork ranges from; the network itself has no CIDR field. No
    "set CIDR on network" API is called.
  * Networks have NO DNS-hostnames / DNS-support toggles — internal DNS is
    served unconditionally by the metadata server. ``read_vpc`` emits
    ``dns_support=null`` / ``dns_hostnames=null`` (honest "no such toggle"),
    never fabricated booleans.
  * ``read_vpc.state`` is routed through ``canonical_state``; a custom-mode
    network has no lifecycle status proto field, so a successful ``get()``
    read-back IS the readiness signal and we emit ``"READY"``.
  * NetworksClient has NO ``set_labels`` and the network ``description`` is
    IMMUTABLE (patch on description returns 400). ``update_tags`` is
    therefore implemented as a REVERSIBLE peering mutation: create an
    ephemeral peer network, ``add_peering`` then ``remove_peering``, and
    read back via ``network_peerings`` to confirm the add took effect.
  * ``update_dns`` is REPURPOSED to a routing-mode toggle (REGIONAL <->
    GLOBAL) via ``NetworksClient.patch`` (the only documented mutable field
    on a network), read back via ``get``.
  * ``NetworksClient.delete`` is async — we wait the global op
    (``delete_network``) then assert ``get_network`` raises ``NotFound``.

The test creates and deletes its OWN network (not the shared setup
network). The ``finally`` block best-effort deletes the main network and
the ephemeral peer network if either is still present after the body runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import canonical_state, resolve_project, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    add_peering,
    delete_network,
    get_network,
    insert_network,
    network_peerings,
    remove_peering,
)
from google.api_core import exceptions as gax
from google.cloud import compute_v1


def _patch_routing_mode(project: str, name: str, routing_mode: str, *, timeout: int = 120) -> None:
    """Patch a network's routing mode (REGIONAL <-> GLOBAL) and wait the op.

    ``routing_config.routing_mode`` is the only documented mutable field on
    a Compute Engine network (description is immutable; there are no
    labels). Mirrors common.network.insert_network's op-wait pattern using
    wait_for_global_op.
    """
    from common.network import wait_for_global_op

    net = compute_v1.Network()
    net.name = name
    routing = compute_v1.NetworkRoutingConfig()
    routing.routing_mode = routing_mode
    net.routing_config = routing

    op = compute_v1.NetworksClient().patch(project=project, network=name, network_resource=net)
    op_name = getattr(op, "name", None) or getattr(op, "operation", "") or ""
    if op_name:
        wait_for_global_op(project, op_name, timeout=timeout)


def _read_routing_mode(project: str, name: str) -> str:
    """Return the live routing mode string off a network."""
    net = get_network(project, name)
    return str(getattr(net.routing_config, "routing_mode", "") or "")


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test Compute Engine VPC CRUD operations")
    parser.add_argument("--region", required=True, help="GCP region (peering routes / op scope)")
    parser.add_argument("--cidr", default="10.99.0.0/16", help="Aggregate CIDR (network has no CIDR)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    # Compute Engine names ARE the API IDs — run-id-suffix everything so
    # parallel runs don't collide on AlreadyExists.
    network_name = unique_suffix("isv-crud-vpc")
    peer_network_name = unique_suffix("isv-crud-peer")
    peering_name = unique_suffix("isv-crud-peering")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "vpc_crud",
        "tests": {
            "create_vpc": {"passed": False},
            "read_vpc": {"passed": False},
            "update_tags": {"passed": False},
            "update_dns": {"passed": False},
            "delete_vpc": {"passed": False},
        },
        "network_id": network_name,
        "vpc_name": network_name,
    }

    # Cleanup trackers for the finally block.
    network_created = False
    peer_created = False
    main_network_deleted = False

    try:
        # 1. create_vpc — insert the custom-mode network, then read back.
        # Stamp network_created BEFORE insert_network: it runs _wait_or_rollback,
        # which on a failed op-wait + failed rollback raises PartialCreateError
        # with the network possibly leaked. The finally cleanup gates on the
        # tracker, so it must be True before the call for a partial create to
        # still reach cleanup (delete on a never-created network is a harmless
        # NotFound no-op). Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)
        get_network(project, network_name)  # raises NotFound if the insert silently failed
        result["tests"]["create_vpc"] = {"passed": True, "vpc_id": network_name}

        # 2. read_vpc — a successful get() is the readiness signal (custom-mode
        # networks have no lifecycle status proto field). Route the value
        # through canonical_state; the empty status maps to a stable string and
        # we surface READY for the existing-and-readable network. DNS toggles do
        # not exist on Compute Engine -> emit null, never fabricated booleans.
        net = get_network(project, network_name)
        raw_state = str(getattr(net, "status", "") or "")
        state = canonical_state(raw_state) if raw_state else "READY"
        result["tests"]["read_vpc"] = {
            "passed": True,
            "state": state,
            "dns_support": None,
            "dns_hostnames": None,
        }

        # 3. update_tags — NetworksClient has no set_labels and description is
        # immutable. Prove mutability with a REVERSIBLE peering add/remove
        # against an ephemeral peer network, confirmed by a real read-back.
        peer_created = True
        insert_network(project, peer_network_name)
        add_peering(project, network_name, peer_network_name, peering_name)
        peerings_after_add = network_peerings(project, network_name)
        add_observed = any(getattr(p, "name", None) == peering_name for p in peerings_after_add)
        remove_peering(project, network_name, peering_name)
        peerings_after_remove = network_peerings(project, network_name)
        remove_observed = all(getattr(p, "name", None) != peering_name for p in peerings_after_remove)
        result["tests"]["update_tags"] = {
            "passed": bool(add_observed and remove_observed),
            "mutation": "temporary_peering_add_remove",
        }

        # 4. update_dns — repurposed to a routing-mode toggle. insert_network
        # creates REGIONAL by default; flip to GLOBAL and read back.
        routing_before = _read_routing_mode(project, network_name)
        target_mode = "GLOBAL" if routing_before != "GLOBAL" else "REGIONAL"
        _patch_routing_mode(project, network_name, target_mode)
        routing_after = _read_routing_mode(project, network_name)
        result["tests"]["update_dns"] = {
            "passed": routing_after == target_mode and routing_after != routing_before,
            "routing_mode_before": routing_before,
            "routing_mode_after": routing_after,
        }

        # 5. delete_vpc — async delete; wait the global op, then assert
        # get_network raises NotFound. Remove the peer first so the network
        # has no dependent peering blocking the delete.
        remove_peering(project, network_name, peering_name)  # idempotent if already gone
        delete_network(project, network_name)
        main_network_deleted = True
        deleted_confirmed = False
        try:
            get_network(project, network_name)
        except gax.NotFound:
            deleted_confirmed = True
        result["tests"]["delete_vpc"] = {"passed": deleted_confirmed}

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Cleanup of resources THIS run created. Drop any lingering peering,
        # then delete the main network (if the delete subtest did not already
        # remove it) and the ephemeral peer network. remove_peering has no
        # retry wrapper and CAN raise, so wrap it individually — a failed
        # peering removal must not skip the network deletes. delete_with_retry
        # never raises and returns False only on exhausted retries; capture
        # every bool so a leaked resource fails the step instead of coexisting
        # with success=True.
        cleanup_errors: list[str] = []
        if network_created and not main_network_deleted:
            try:
                remove_peering(project, network_name, peering_name)
            except Exception as cleanup_exc:
                print(f"Cleanup error: {cleanup_exc}", file=sys.stderr)
                cleanup_errors.append(f"peering {peering_name} on {network_name}")
            print(f"Cleanup: deleting main network {network_name}", file=sys.stderr)
            if not delete_with_retry(delete_network, project, network_name, resource_desc=f"network {network_name}"):
                cleanup_errors.append(f"network {network_name}")
        if peer_created:
            print(f"Cleanup: deleting peer network {peer_network_name}", file=sys.stderr)
            if not delete_with_retry(
                delete_network, project, peer_network_name, resource_desc=f"network {peer_network_name}"
            ):
                cleanup_errors.append(f"network {peer_network_name}")
        if cleanup_errors:
            result.setdefault("cleanup_errors", []).extend(cleanup_errors)
            result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
