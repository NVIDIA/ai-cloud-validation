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

"""Create the shared Compute Engine VPC for network validation (setup phase).

Translates the AWS provider's ``create_vpc`` workflow to Compute Engine.
Documented divergences:

  * Networks own NO CIDR — ``--cidr`` is the aggregate the stub carves
    subnetwork ranges from. ``cidr`` echoes the create-time arg; no
    "set CIDR on network" API is called.
  * Subnetworks are REGIONAL (one subnetwork covers every zone in its
    region). There is no zone/AZ field; the contract's ``az`` is populated
    from REAL zones in the configured region (RegionsClient.get(region)),
    cycling one zone per emitted subnet entry.
  * There is NO internet gateway resource — custom-mode networks ship with
    an implicit default route via ``default-internet-gateway``. The
    ``internet_gateway_id`` field is OMITTED.
  * There is NO per-VPC route table resource — ``route_table_id`` is
    OMITTED.
  * Firewall rules are project-scoped + network-bound. SSH (tcp/22) ingress
    is restricted to the operator-trusted source ranges
    (``NETWORK_FIREWALL_TRUST_IP``) — never 0.0.0.0/0 — and that rule's name is
    emitted as ``security_group_id``. ICMP is a SEPARATE intra-VPC rule (scoped
    to the aggregate CIDR + the trusted source) so instance-to-instance
    connectivity probes keep working without widening the SSH restriction.
  * There is NO DHCP-options resource — ``dhcp_options`` is synthesized from
    the metadata-server resolver (169.254.169.254) via dhcp_options_payload.
  * ``Network`` / ``Subnetwork`` / ``Firewall`` protos have NO ``labels``
    field; provenance is the immutable ``description`` marker only.

This is the SHARED setup network downstream steps consume. It is NOT torn
down here — the teardown step destroys it. The ``finally`` block only
performs a best-effort partial-failure cleanup of what THIS run created
(gated on the created flags) so a mid-creation failure does not leak.

The adopt-or-create contract: if a network with the exact run-id-suffixed
name already exists carrying the ISV ownership marker, adopt it and set
``network_created=False`` (teardown then preserves it). Otherwise create it
and set ``network_created=True``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    build_firewall,
    carve_subnet_cidrs,
    cycle_zones,
    delete_firewall,
    delete_network,
    delete_subnetwork,
    dhcp_options_payload,
    get_network,
    insert_firewall,
    insert_network,
    insert_subnetwork,
    make_allowed,
    network_has_isv_ownership,
    region_zones,
    resolve_trusted_firewall_sources,
    usable_ip_count,
)
from google.api_core import exceptions as gax

# Two subnets in two zones — mirrors the AWS provider's two-AZ shape and
# satisfies NetworkProvisionedCheck (require_subnets=true, min_subnets=2).
SUBNET_COUNT = 2


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Create the shared GCP VPC for network validation")
    parser.add_argument("--name", default="isv-shared-vpc", help="Network name prefix")
    parser.add_argument("--region", required=True, help="GCP region for the regional subnetworks")
    parser.add_argument("--cidr", default="10.0.0.0/16", help="Aggregate CIDR to carve subnet ranges from")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    # Compute Engine names ARE the API IDs — run-id-suffix every created
    # resource so parallel runs don't collide on AlreadyExists.
    network_name = unique_suffix(args.name)
    firewall_name = unique_suffix(f"{args.name}-fw")
    icmp_firewall_name = unique_suffix(f"{args.name}-icmp-fw")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "network_id": network_name,
        "cidr": args.cidr,
        "subnets": [],
        "security_group_id": firewall_name,
        "dhcp_options": None,
        "network_created": False,
        "region": args.region,
        "name": args.name,
    }

    # Per-resource cleanup trackers for the partial-failure path only.
    network_created = False
    created_subnet_names: list[str] = []
    firewall_created = False
    icmp_firewall_created = False

    try:
        # Resolve the operator-trusted SSH source range(s) BEFORE creating
        # anything so an unset/invalid/broad NETWORK_FIREWALL_TRUST_IP fails
        # closed with no half-created network to clean up (there is no fallback
        # source range for SSH/RDP ingress).
        trusted_ssh_sources = resolve_trusted_firewall_sources()

        # Resolve real zones in the configured region BEFORE creating
        # anything so an invalid/unauthorized region fails fast (no
        # half-created network to clean up).
        zones = region_zones(project, args.region)
        if not zones:
            raise RuntimeError(f"region {args.region!r} reports no zones; cannot populate subnet az field")
        subnet_cidrs = carve_subnet_cidrs(args.cidr, SUBNET_COUNT)
        subnet_zones = cycle_zones(zones, SUBNET_COUNT)

        # 1. Adopt-or-create the custom-mode network.
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
            print(f"  adopting pre-existing ISV-owned network {network_name}", file=sys.stderr)
            adopted = True
        else:
            # Stamp the created tracker AT OP-INITIATION (before the insert
            # helper's internal op-wait). The name is deterministic, so if
            # the insert ack lands but the op-wait — and its best-effort
            # rollback delete — both fail, teardown must still see
            # network_created=True to delete the leaked network (teardown
            # gates the shared-VPC delete strictly on this flag and does not
            # otherwise enumerate the network itself). A delete attempt on a
            # never-created network is a harmless NotFound no-op.
            network_created = True
            result["network_created"] = True
            insert_network(project, network_name)

        # 2. Create the regional subnetworks, populating az from real zones.
        for idx, (cidr, zone) in enumerate(zip(subnet_cidrs, subnet_zones)):
            subnet_name = unique_suffix(f"{args.name}-subnet-{idx}")
            # Record the subnet name BEFORE insert_subnetwork (stamp-before, as
            # for the network@142 and firewall@182 above): the helper runs
            # _wait_or_rollback and on a terminal partial create raises before
            # this line, so the name must already be in created_subnet_names for
            # the finally cleanup to delete the leaked subnet.
            created_subnet_names.append(subnet_name)
            # Enable VPC Flow Logs on every test subnet so the SDN
            # latency/perf step has a real, customer-visible telemetry source
            # to detect. Without a configured source that step can only ever
            # observe an empty VPC and would otherwise have to pass on absence
            # of telemetry, which validates a missing source as success.
            insert_subnetwork(project, args.region, subnet_name, network_name, cidr, enable_flow_logs=True)
            result["subnets"].append(
                {
                    "subnet_id": subnet_name,
                    "cidr": cidr,
                    "az": zone,
                    # Compute Engine attaches external IPs per-NIC at launch
                    # via accessConfigs, never as a subnet attribute. Emit
                    # false; reconcile via VpcIpConfigCheck.auto_assign_ip_mode
                    # =instance (provider config override).
                    "auto_assign_public_ip": False,
                    "available_ips": usable_ip_count(cidr),
                }
            )

        # 3a. SSH INGRESS firewall (emitted as security_group_id). SSH (tcp/22)
        # is an admin port: its source is restricted to the operator-trusted
        # ranges resolved above, NEVER 0.0.0.0/0.
        # An allow rule MUST carry at least one Allowed with I_p_protocol set
        # (empty allowed[] -> HTTP 400).
        fw = build_firewall(
            firewall_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=[make_allowed("tcp", ["22"])],
            source_ranges=trusted_ssh_sources,
        )
        # Stamp the created tracker BEFORE the insert (mirrors the network
        # stamp at step 1): the name is deterministic, so if the insert ack
        # lands but the op-wait and its best-effort rollback both fail
        # (PartialCreateError), the cleanup-on-failure path must still see
        # firewall_created=True to delete the leaked rule. A delete on a
        # never-created firewall is a harmless NotFound no-op.
        firewall_created = True
        insert_firewall(project, fw)

        # 3b. ICMP INGRESS firewall — a SEPARATE rule so the SSH restriction
        # above is not widened. ICMP is not an admin port, so the trust-IP
        # policy does not apply; scope it to the VPC aggregate CIDR (for
        # instance-to-instance connectivity probes) plus the operator-trusted
        # source (so an operator host can ping the probe VMs) rather than the
        # whole internet. Teardown enumerates every run-owned firewall on the
        # network, so this rule is cleaned up without a dedicated emit.
        icmp_sources = sorted({args.cidr, *trusted_ssh_sources})
        icmp_fw = build_firewall(
            icmp_firewall_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=[make_allowed("icmp")],
            source_ranges=icmp_sources,
        )
        icmp_firewall_created = True
        insert_firewall(project, icmp_fw)

        # 4. DHCP options — synthesized from the metadata-server resolver;
        # no DHCP-options API exists. domain_name is OMITTED (the schema
        # types it as string, so null fails validation).
        result["dhcp_options"] = dhcp_options_payload(network_name)

        result["success"] = True
        if adopted:
            print(f"Adopted shared network {network_name} ({len(result['subnets'])} subnets)", file=sys.stderr)
        else:
            print(f"Created shared network {network_name} ({len(result['subnets'])} subnets)", file=sys.stderr)

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
        # Partial-failure cleanup ONLY. The happy-path network/subnets/
        # firewall are the shared setup resources torn down by the teardown
        # step — never delete those here. Gate strictly on the created
        # flags so an adopted operator network is preserved. Delete in
        # dependency order: firewall + subnets first, then the network.
        try:
            if firewall_created:
                print(f"Cleanup-on-failure: deleting firewall {firewall_name}", file=sys.stderr)
                delete_with_retry(delete_firewall, project, firewall_name, resource_desc=f"firewall {firewall_name}")
            if icmp_firewall_created:
                print(f"Cleanup-on-failure: deleting firewall {icmp_firewall_name}", file=sys.stderr)
                delete_with_retry(
                    delete_firewall, project, icmp_firewall_name, resource_desc=f"firewall {icmp_firewall_name}"
                )
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
