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

"""Create the run-scoped observability network + subnetwork (setup phase).

Translates the AWS observability oracle's ``create_network`` (an isolated VPC +
subnet for the log probes) onto Compute Engine. Documented divergences:

  * A Compute Engine network owns NO CIDR — ``--cidr`` is the aggregate the stub
    carves the subnetwork range from; a custom-mode network
    (``auto_create_subnetworks=false``) is created and one REGIONAL subnetwork is
    inserted (subnetworks own the CIDR and cover every zone in the region).
  * There is no standalone flow-log resource: this step creates subnetworks with
    logging OFF and the downstream ``enable_vpc_flow_logs`` step patches logging
    ON. Keeping the two concerns separate lets ``flow_logs_created`` be a truthful
    "this run patched a run-owned subnetwork" signal for teardown.
  * No SSH firewall is created here — ``launch_host`` owns the tcp/22 rule (whose
    source is the operator-trusted ``NETWORK_FIREWALL_TRUST_IP``, never
    0.0.0.0/0) so the firewall lifecycle stays with the host that needs it.

Emits the provider-neutral setup evidence consumed by later steps:

    {
        "success":         bool,
        "platform":        "observability",
        "network_id":      str,        # Compute Engine Network.name
        "cidr":            str,        # aggregate arg (networks have no CIDR)
        "subnets":         [ {subnet_id, cidr, az, available_ips}, ... ],
        "created_subnets": [str, ...], # EXACT allowlist this run created — the
                                       # only subnets teardown_network deletes and
                                       # enable_vpc_flow_logs is authorized to patch
        "network_created": bool,       # true only after this run's insert accepted
        ...
    }

Adopt-or-create: if a network with the exact run-id-suffixed name already exists
carrying the ISV ownership marker, adopt it (``network_created=False`` so teardown
preserves it); if it exists WITHOUT the marker, refuse to adopt; otherwise create
it (``network_created=True``). Run-ownership is tracked PER RESOURCE, so an adopted
subnet on a reused network stays absent from ``created_subnets`` and is preserved
implicitly at teardown.

AWS reference implementation:
    ../../aws/scripts/network/create_vpc.py (create_network reuses the network stub)
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, short_name, unique_suffix
from common.errors import classify_gcp_error, delete_with_retry, handle_gcp_errors
from common.network import (
    carve_subnet_cidrs,
    cycle_zones,
    delete_network,
    delete_subnetwork,
    get_network,
    get_subnetwork,
    insert_network,
    insert_subnetwork,
    network_has_isv_ownership,
    region_zones,
    subnetwork_has_isv_ownership,
    usable_ip_count,
)
from google.api_core import exceptions as gax

# One subnetwork is enough for the single syslog-probe host; the flow-log
# checks read every subnetwork bound to the network in the region.
SUBNET_COUNT = 1
# Aggregate is carved into /24 ranges (256 addresses -> 252 usable, well above
# the single host's needs).
SUBNET_PREFIX = 24


@handle_gcp_errors
def main() -> int:
    """Create the observability network + subnetwork and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Create the run-scoped GCP observability network")
    parser.add_argument("--name", default="isv-observability-net", help="Network name prefix (run-id suffixed)")
    parser.add_argument("--region", required=True, help="GCP region for the regional subnetwork(s)")
    parser.add_argument("--cidr", default="10.240.0.0/16", help="Aggregate CIDR to carve the subnet range from")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    # Compute Engine names ARE the API IDs. A run-id-only suffix is NOT enough:
    # parallel step-isolation jobs share a single RUN_ID, so a run-id-only name
    # is identical across those sibling jobs and they collide on AlreadyExists
    # (or, worse, silently share lifecycle state) — the get-before-insert adopt
    # path below cannot recover from a concurrent insert conflict. Fold a
    # per-invocation discriminator (4 hex chars) BETWEEN the base and the run-id
    # suffix so every invocation gets a fresh name; the run id stays TERMINAL so
    # the run-id-scoped orphan sweep (which matches names ending in the run id)
    # still recognizes them. The full resulting names are emitted below and
    # forwarded verbatim to teardown, which never reconstructs them.
    disc = secrets.token_hex(2)  # 4 hex chars, fresh per invocation
    network_name = unique_suffix(f"{args.name}-{disc}")

    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "create_network",
        "network_id": network_name,
        "cidr": args.cidr,
        "subnets": [],
        "created_subnets": [],
        "network_created": False,
        "region": args.region,
        "name": args.name,
    }

    # Per-resource cleanup trackers for the partial-failure path only.
    network_created = False
    created_subnet_names: list[str] = []

    try:
        # Resolve real zones in the configured region BEFORE creating anything so
        # an invalid / unauthorized region fails fast (no half-created network to
        # clean up). The subnetwork is regional, but the suite's az evidence wants
        # a zone-shaped value populated from a REAL zone in the region.
        zones = region_zones(project, args.region)
        if not zones:
            raise RuntimeError(f"region {args.region!r} reports no zones; cannot populate subnet az evidence")
        subnet_cidrs = carve_subnet_cidrs(args.cidr, SUBNET_COUNT, new_prefix=SUBNET_PREFIX)
        subnet_zones = cycle_zones(zones, SUBNET_COUNT)

        # 1. Adopt-or-create the custom-mode network. Verified-reuse: an existing
        # network is adopted ONLY when it carries the ISV ownership marker AND
        # every invariant this run would create matches on live read-back
        # (custom-mode + REGIONAL routing). A mismatch is rejected WITHOUT
        # recording created ownership (network_created stays False) so teardown
        # never deletes a stale or foreign network.
        adopted = False
        try:
            existing = get_network(project, network_name)
        except gax.NotFound:
            existing = None
        if existing is not None:
            if not network_has_isv_ownership(existing):
                raise RuntimeError(
                    f"network {network_name!r} exists in {project} without the ISV ownership marker; refusing to adopt"
                )
            if bool(existing.auto_create_subnetworks):
                raise RuntimeError(
                    f"network {network_name!r} exists but is auto-mode (auto_create_subnetworks=true); "
                    "this run requires a custom-mode network — refusing to adopt a mismatched network"
                )
            existing_mode = getattr(existing.routing_config, "routing_mode", "") or ""
            if existing_mode != "REGIONAL":
                raise RuntimeError(
                    f"network {network_name!r} exists but routing_mode={existing_mode!r} != 'REGIONAL'; "
                    "refusing to adopt a mismatched network"
                )
            print(f"  adopting pre-existing ISV-owned network {network_name}", file=sys.stderr)
            adopted = True
        else:
            # Stamp the created tracker via on_accepted — fired after the
            # synchronous insert is ACCEPTED but before the op-wait. A synchronous
            # conflict raises before acceptance, so we never claim a foreign
            # network; a wait-side failure still leaves network_created=True so
            # teardown deletes the accepted-but-unconfirmed network (a delete on a
            # never-created network is a harmless NotFound no-op).
            def _mark_network_created() -> None:
                nonlocal network_created
                network_created = True
                result["network_created"] = True

            insert_network(project, network_name, on_accepted=_mark_network_created)

        # 2. Adopt-or-create the regional subnetwork(s) with logging OFF
        # (enable_vpc_flow_logs patches it on). Dependent-readiness: the network
        # insert op above reached DONE, but the network may not yet be usable as a
        # subnet parent in the brief eventual-consistency window after DONE, so the
        # subnet insert can be rejected pre-acceptance with HTTP 400 resourceNotReady.
        # insert_subnetwork owns a bounded retry gated ONLY on that not-ready
        # response (the GCP analog of the AWS oracle's vpc_available wait), so no
        # broad conflict/5xx retry is added here. Each subnet name is deterministic,
        # so a rerun on an adopted network may find it already present: verified-
        # adopt an exact match (ISV marker + network binding + CIDR) WITHOUT
        # recording created ownership (it stays out of created_subnets and is
        # preserved implicitly at teardown), and reject a mismatch. Only a freshly
        # inserted subnet is stamped into created_subnets — via on_accepted, after
        # synchronous acceptance but before the op-wait, so a synchronous conflict
        # never claims a foreign subnet while a wait-side failure still hands
        # teardown_network the subnet in --created-subnets. created_subnet_names
        # tracks the same set so the forwarded allowlist and local cleanup can
        # never diverge.
        for idx, (cidr, zone) in enumerate(zip(subnet_cidrs, subnet_zones, strict=False)):
            subnet_name = unique_suffix(f"{args.name}-{disc}-subnet-{idx}")
            try:
                existing_subnet = get_subnetwork(project, args.region, subnet_name)
            except gax.NotFound:
                existing_subnet = None
            if existing_subnet is not None:
                if not subnetwork_has_isv_ownership(existing_subnet):
                    raise RuntimeError(
                        f"subnetwork {subnet_name!r} exists in {project}/{args.region} without the ISV "
                        "ownership marker; refusing to adopt"
                    )
                if short_name(existing_subnet.network) != network_name:
                    raise RuntimeError(
                        f"subnetwork {subnet_name!r} is bound to {short_name(existing_subnet.network)!r}, "
                        f"expected {network_name!r}; refusing to adopt a mismatched subnet"
                    )
                if existing_subnet.ip_cidr_range != cidr:
                    raise RuntimeError(
                        f"subnetwork {subnet_name!r} CIDR {existing_subnet.ip_cidr_range!r} != requested "
                        f"{cidr!r}; refusing to adopt a mismatched subnet"
                    )
                print(f"  adopting pre-existing ISV-owned subnetwork {subnet_name}", file=sys.stderr)
            else:

                def _mark_subnet_created(created_name: str = subnet_name) -> None:
                    created_subnet_names.append(created_name)
                    result["created_subnets"].append(created_name)

                insert_subnetwork(
                    project,
                    args.region,
                    subnet_name,
                    network_name,
                    cidr,
                    enable_flow_logs=False,
                    on_accepted=_mark_subnet_created,
                )
            result["subnets"].append(
                {
                    "subnet_id": subnet_name,
                    "cidr": cidr,
                    "az": zone,
                    # Compute Engine attaches external IPs per-NIC at launch via
                    # accessConfigs, never as a subnet attribute.
                    "auto_assign_public_ip": False,
                    "available_ips": usable_ip_count(cidr),
                }
            )

        # 3. Confirm observable completion: the subnetwork op reporting DONE is
        # the readiness gate (Subnetwork.state is empty for a fresh custom-mode
        # subnet even after DONE), and get_subnetwork proves the CIDR/network
        # binding round-tripped rather than trusting the insert ack alone.
        for subnet in result["subnets"]:
            live = get_subnetwork(project, args.region, subnet["subnet_id"])
            if short_name(live.network) != network_name:
                raise RuntimeError(
                    f"subnetwork {subnet['subnet_id']!r} bound to {short_name(live.network)!r}, "
                    f"expected {network_name!r}"
                )
            if live.ip_cidr_range != subnet["cidr"]:
                raise RuntimeError(
                    f"subnetwork {subnet['subnet_id']!r} CIDR {live.ip_cidr_range!r} != requested {subnet['cidr']!r}"
                )

        result["success"] = True
        verb = "Adopted" if adopted else "Created"
        print(f"{verb} observability network {network_name} ({len(result['subnets'])} subnet(s))", file=sys.stderr)

    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        result.setdefault("error_type", error_type)
        result["error"] = error_msg
        result["success"] = False
        # Partial-failure cleanup ONLY — gate strictly on the created flags so an
        # adopted operator network / subnet is preserved. Delete dependents
        # (subnets) before the network.
        try:
            for subnet_name in created_subnet_names:
                print(f"Cleanup-on-failure: deleting subnetwork {subnet_name}", file=sys.stderr)
                delete_with_retry(
                    delete_subnetwork,
                    project,
                    args.region,
                    subnet_name,
                    resource_desc=f"subnetwork {subnet_name}",
                )
            if network_created:
                print(f"Cleanup-on-failure: deleting network {network_name}", file=sys.stderr)
                delete_with_retry(delete_network, project, network_name, resource_desc=f"network {network_name}")
        except Exception as cleanup_exc:
            print(f"Cleanup-on-failure error: {cleanup_exc}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
