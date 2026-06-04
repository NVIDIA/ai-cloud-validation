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

"""Teardown the shared Compute Engine VPC + dependents (teardown phase).

Translates the AWS provider's ``teardown`` workflow to Compute Engine.
Documented divergences:

  * Instances are found via ``InstancesClient.aggregated_list`` filtered on
    ``networkInterfaces[*].network`` (no project-wide "list instances in
    network" call). Each is zonal — delete in its own zone.
  * VPC peering is a property of the network, not a separate resource;
    remove each peering by name before deleting the network.
  * There is NO internet-gateway resource — ``deleted.internet_gateways`` is
    emitted as ``[]`` with an explanatory message; we never call delete on
    ``default-internet-gateway``.
  * Subnetworks are REGIONAL — enumerate them across the ``--region`` and
    delete each.
  * GCE auto-creates ``default-route-<hex>`` system routes (next_hop_network
    set) that CANNOT be deleted (API 400 "The local route cannot be
    deleted"); they are reaped automatically on subnet delete. Skip them via
    ``is_auto_route`` and delete only the rest.
  * The local SSH PEM/.pub pair is the only key artifact (no server-side key
    resource). Delete it by the exact ``--key-file`` path forwarded from the
    create step, gated on ``--key-created`` — and run this WHETHER OR NOT the
    cloud preflight returned NotFound (local-artifact cleanup is
    unconditional within the gating flag).

Verified-reuse: the network delete is gated on ``--network-created`` so an
adopted operator network survives teardown. Cleanup provenance: when the
network was ADOPTED (``network_created=false``) the network may carry
operator-owned, pre-existing dependents, so dependent cleanup is restricted
to resources this suite created — those carrying the ``createdby=isvtest``
marker in their immutable ``description``. Peerings expose no description
provenance, so on the adopted path they are preserved (not broad-removed).
When this run CREATED the network (``network_created=true``) every dependent
in that run-id-suffixed network is run-owned, so all are deleted. Idempotency:
NotFound counts as success everywhere. Every per-resource ``delete_with_retry`` bool is folded
into ``result['success']`` via ``all([...])`` (StepSuccessCheck reads
``success``). Resource ENUMERATION is likewise guarded: a NotFound list/read
is idempotent (already gone), but any other enumeration failure (auth /
transient / API) is recorded in ``cleanup_errors`` and fails the aggregate —
otherwise an empty ``delete_oks`` (``all([]) == True``) would report a green
teardown after silently skipping dependent cleanup.

Usage:
    python teardown.py --vpc-id <network> --region <region> \
        --network-created true|false [--key-file <path>] [--key-name <name>] \
        [--key-created true|false] [--skip-destroy] [--project <id>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import delete_local_keypair, resolve_project
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    ISV_OWNERSHIP_MARKER,
    delete_firewall,
    delete_instance,
    delete_network,
    delete_route,
    delete_subnetwork,
    instances_in_network,
    is_auto_route,
    list_firewalls_for_network,
    list_routes_for_network,
    list_subnetworks_for_network,
    network_peerings,
    remove_peering,
)
from google.api_core import exceptions as gax

# Inputs that mean "no artifact tracked" / "false". The provider config
# wires inter-step Jinja args with non-empty sentinels ('none' for
# paths/IDs, 'false' for bools) so the orchestrator does not collapse the
# flag/value pair.
_FALSY_SENTINELS = {"", "none", "null", "false"}


def _truthy(arg: str | None) -> bool:
    """Per-arg sentinel check. Treats '' / 'none' / 'null' / 'false' as falsy."""
    if arg is None:
        return False
    return arg.strip().lower() not in _FALSY_SENTINELS


def _run_owned(resource: Any) -> bool:
    """True iff a dependent carries this suite's ISV ownership marker.

    Every resource the GCP network scripts create stamps ``createdby=isvtest``
    into its immutable ``description`` (Network/Subnetwork/Firewall/Instance
    protos have no ``labels`` field). On the ADOPTED-network path this gates
    deletion so operator-owned, pre-existing dependents on the adopted network
    are preserved (cleanup-provenance / verified-reuse safety).
    """
    return ISV_OWNERSHIP_MARKER in (getattr(resource, "description", "") or "").lower()


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown the shared GCP VPC + dependents")
    parser.add_argument("--vpc-id", required=True, help="Network short name to delete")
    parser.add_argument("--region", required=True, help="GCP region for regional subnetwork enumeration")
    parser.add_argument(
        "--network-created",
        default="false",
        help="Bool sentinel forwarded from create_network.network_created; gates the VPC delete",
    )
    parser.add_argument(
        "--key-file",
        default="none",
        help="Local SSH PEM path forwarded from the create step (e.g. dhcp_ip_test.key_file)",
    )
    parser.add_argument(
        "--key-name",
        default="none",
        help="Key stem forwarded from the create step (informational; deletion uses --key-file)",
    )
    parser.add_argument(
        "--key-created",
        default="false",
        help="Bool sentinel forwarded from the create step; gates local key deletion",
    )
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        help="Short-circuit to success (preserve cloud + local state) BEFORE resolving auth",
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "resources_destroyed": False,
        "network_id": args.vpc_id,
        "deleted": {
            "instances": [],
            "firewalls": [],
            "subnets": [],
            "routes": [],
            "peerings": [],
            "internet_gateways": [],
            "vpc": None,
        },
        "message": "",
    }

    # Honor skip BEFORE resolving auth so an expired-ADC environment can
    # still no-op cleanly. Run later with --phase teardown when ready.
    skip_destroy = args.skip_destroy or os.environ.get("GCP_NETWORK_SKIP_TEARDOWN", "").lower() == "true"
    if skip_destroy:
        result["success"] = True
        result["message"] = "Destroy skipped (--skip-destroy flag or GCP_NETWORK_SKIP_TEARDOWN=true)"
        print(json.dumps(result, indent=2, default=str))
        return 0

    project = resolve_project(args.project)
    network_name = args.vpc_id
    network_created = _truthy(args.network_created)
    key_created = _truthy(args.key_created)
    key_file = args.key_file if _truthy(args.key_file) else None

    # Every cloud delete-with-retry bool folds into success. NotFound is
    # success inside delete_with_retry, so a fully-absent network still
    # yields success=True (idempotent teardown).
    delete_oks: list[bool] = []
    # Non-NotFound enumeration failures are recorded here and folded into
    # success. An empty `delete_oks` would otherwise make `all([])` true, so
    # a transient/auth list failure before any delete could report a green
    # teardown after deleting nothing (leaking dependent resources). NotFound
    # stays idempotent success (the network/resource is already gone).
    cleanup_errors: list[str] = []

    def _enumerate(label: str, fn: Any, *fn_args: Any) -> list[Any]:
        """List network-scoped resources; never raise.

        NotFound means the network/resource is already gone -> idempotent
        empty list, no failure recorded. Any other error (auth, transient,
        API) is a cleanup failure: it is recorded so dependent cleanup is not
        silently skipped behind a green result.
        """
        try:
            return list(fn(*fn_args))
        except gax.NotFound:
            print(f"  {label} enumeration: already gone (NotFound)", file=sys.stderr)
            return []
        except Exception as e:
            print(f"  warn: {label} enumeration failed: {e}", file=sys.stderr)
            cleanup_errors.append(f"{label} enumeration failed: {e}")
            return []

    # On an ADOPTED network (network_created=false) the network and its
    # dependents may be operator-owned/pre-existing, so dependent cleanup is
    # restricted to resources this run created (verifiable description-marker
    # provenance). When this run created the network, every dependent in the
    # run-id-suffixed network is run-owned and is deleted unconditionally.
    adopted = not network_created

    # 1. Instances on the network (aggregated_list across all zones).
    instances = _enumerate("instance", instances_in_network, project, network_name)
    for inst_zone, inst in instances:
        name = inst.name
        if adopted and not _run_owned(inst):
            print(f"  preserving unowned instance {name} on adopted network", file=sys.stderr)
            continue
        print(f"Deleting instance {name} in {inst_zone}...", file=sys.stderr)
        ok = delete_with_retry(
            delete_instance,
            project,
            inst_zone,
            name,
            resource_desc=f"instance {name}@{inst_zone}",
        )
        delete_oks.append(ok)
        if ok:
            result["deleted"]["instances"].append(name)

    # 2. Peerings (a property of the network — remove each by name). NotFound
    # on the network read is idempotent success (already gone); other errors
    # are recorded as cleanup failures and do not block sibling cleanup.
    # NetworkPeering exposes no description, so on the adopted path it has no
    # verifiable run-ownership: preserve operator peerings rather than
    # broad-removing them.
    peerings = [] if adopted else _enumerate("peering", network_peerings, project, network_name)
    if adopted:
        print("  preserving peerings on adopted network (no ownership provenance)", file=sys.stderr)
    for peering in peerings:
        pname = getattr(peering, "name", None)
        if not pname:
            continue
        print(f"Removing peering {pname}...", file=sys.stderr)
        ok = delete_with_retry(
            remove_peering,
            project,
            network_name,
            pname,
            resource_desc=f"peering {pname}",
        )
        delete_oks.append(ok)
        if ok:
            result["deleted"]["peerings"].append(pname)

    # 3. Firewalls bound to the network.
    firewalls = _enumerate("firewall", list_firewalls_for_network, project, network_name)
    for fw in firewalls:
        if adopted and not _run_owned(fw):
            print(f"  preserving unowned firewall {fw.name} on adopted network", file=sys.stderr)
            continue
        print(f"Deleting firewall {fw.name}...", file=sys.stderr)
        ok = delete_with_retry(
            delete_firewall,
            project,
            fw.name,
            resource_desc=f"firewall {fw.name}",
        )
        delete_oks.append(ok)
        if ok:
            result["deleted"]["firewalls"].append(fw.name)

    # 4. Subnetworks (regional) — iterate the configured region.
    subnets = _enumerate("subnetwork", list_subnetworks_for_network, project, args.region, network_name)
    for sub in subnets:
        if adopted and not _run_owned(sub):
            print(f"  preserving unowned subnetwork {sub.name} on adopted network", file=sys.stderr)
            continue
        print(f"Deleting subnetwork {sub.name} in {args.region}...", file=sys.stderr)
        ok = delete_with_retry(
            delete_subnetwork,
            project,
            args.region,
            sub.name,
            resource_desc=f"subnetwork {sub.name}",
        )
        delete_oks.append(ok)
        if ok:
            result["deleted"]["subnets"].append(sub.name)

    # 5. Routes — skip GCE auto-routes (default-route-* / next_hop_network);
    # they cannot be deleted (API 400) and are reaped on subnet delete.
    routes = _enumerate("route", list_routes_for_network, project, network_name)
    for route in routes:
        if is_auto_route(route):
            print(f"  skipping auto-route {route.name} (reaped on subnet delete)", file=sys.stderr)
            continue
        if adopted and not _run_owned(route):
            print(f"  preserving unowned route {route.name} on adopted network", file=sys.stderr)
            continue
        print(f"Deleting route {route.name}...", file=sys.stderr)
        ok = delete_with_retry(
            delete_route,
            project,
            route.name,
            resource_desc=f"route {route.name}",
        )
        delete_oks.append(ok)
        if ok:
            result["deleted"]["routes"].append(route.name)

    # 6. Internet gateway — no Compute Engine analog (implicit
    # default-internet-gateway). Emit an empty list with a note.
    result["deleted"]["internet_gateways"] = []

    # 7. VPC delete — gated on --network-created so an adopted operator
    # network is preserved (verified-reuse).
    network_note = ""
    if network_created:
        print(f"Deleting network {network_name}...", file=sys.stderr)
        ok = delete_with_retry(
            delete_network,
            project,
            network_name,
            resource_desc=f"network {network_name}",
        )
        delete_oks.append(ok)
        if ok:
            result["deleted"]["vpc"] = network_name
        network_note = "network deleted" if ok else "network delete failed"
    else:
        network_note = "network preserved (network_created=false; adopted operator network not deleted)"
        print(f"  {network_note}", file=sys.stderr)
        result.setdefault("warnings", []).append(network_note)

    # 8. Local SSH key pair — gated on key_created, UNCONDITIONAL w.r.t. the
    # cloud-side NotFound idempotency: local-artifact cleanup runs whether
    # or not the cloud reads returned NotFound.
    key_ok = True
    if key_created and key_file:
        priv_present = os.path.exists(key_file)
        pub_present = os.path.exists(key_file + ".pub")
        if priv_present or pub_present:
            print(f"Deleting local SSH key pair: {key_file} (+ .pub)", file=sys.stderr)
            key_ok = delete_local_keypair(key_file)
            if key_ok:
                result.setdefault("deleted", {}).setdefault("key_files", [])
                if priv_present:
                    result["deleted"]["key_files"].append(key_file)
                if pub_present:
                    result["deleted"]["key_files"].append(key_file + ".pub")
        else:
            print(f"  local SSH key pair already absent: {key_file} + .pub", file=sys.stderr)
    else:
        print("  skipping local key cleanup (key_created=false or no path)", file=sys.stderr)

    # StepSuccessCheck reads `success`: fold every cloud delete bool + the
    # local-key bool + the enumeration-failure signal into the aggregate. An
    # empty network (no resources found, all NotFound) still yields
    # success=True; but a non-NotFound enumeration failure (auth/transient/
    # API) is a cleanup failure even when `delete_oks` is empty, so we do not
    # report a green teardown after silently skipping dependent cleanup.
    enumeration_ok = not cleanup_errors
    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors
    deletes_ok = all(delete_oks)
    result["success"] = bool(deletes_ok and key_ok and enumeration_ok)
    result["resources_destroyed"] = result["success"]
    if result["success"]:
        result["message"] = (
            f"VPC and dependents destroyed ({network_note}); internet_gateways: no Compute Engine analog"
        )
    else:
        result["message"] = (
            f"Cleanup partial: cloud_deletes_ok={deletes_ok}, key_ok={key_ok}, "
            f"enumeration_ok={enumeration_ok} ({network_note})"
        )

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
