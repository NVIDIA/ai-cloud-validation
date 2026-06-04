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

"""Inspect the shared VPC's IP configuration on Compute Engine (read-only).

Translates the AWS provider's ``vpc_ip_config_test`` workflow to Compute
Engine. This step does NOT create the network — it inspects the shared
``--vpc-id`` network created by the create_network step. Documented
divergences:

  * Networks own NO CIDR. The contract's top-level ``cidr`` is the
    create-time aggregate forwarded from the create_network step via
    ``--cidr`` (the same value subnets were carved from), NOT a value
    derived from the live subnet ranges. Emitting the create-time
    aggregate keeps the output contract/oracle-shaped (mirrors the AWS
    provider reading ``vpc.CidrBlock``) and still satisfies
    VpcIpConfigCheck's subnet_of relationship (each subnet cidr is
    subnet_of the aggregate it was carved from).
  * Subnetworks are REGIONAL — no zone/AZ field. The ``az`` field is
    populated from REAL zones in the configured region (RegionsClient.get),
    cycling one zone per subnet entry.
  * External IPs are attached per-NIC at launch via accessConfigs, never as
    a subnet attribute — ``auto_assign_public_ip`` is emitted as false;
    reconcile via VpcIpConfigCheck.auto_assign_ip_mode=instance.
  * There is NO DHCP-options resource — ``dhcp_options`` is synthesized via
    dhcp_options_payload (metadata-server resolver 169.254.169.254). The
    payload OMITS ``domain_name`` (the schema types it as string; null
    fails validation) rather than emitting a null.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, short_name
from common.errors import handle_gcp_errors
from common.network import (
    cycle_zones,
    dhcp_options_payload,
    get_network,
    list_subnetworks_for_network,
    region_zones,
    usable_ip_count,
)


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Compute Engine VPC IP configuration (read-only)")
    parser.add_argument("--vpc-id", required=True, help="Network short name to inspect")
    parser.add_argument("--region", required=True, help="GCP region of the regional subnetworks")
    parser.add_argument(
        "--cidr",
        default="10.0.0.0/16",
        help="Create-time aggregate CIDR forwarded from create_network (the network owns no CIDR)",
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "vpc_ip_config",
        "network_id": args.vpc_id,
        "cidr": None,
        "subnets": [],
        "dhcp_options": None,
    }

    try:
        # Read the network (raises NotFound -> classified api_error) so the
        # inspection fails honestly when the shared VPC is absent.
        net = get_network(project, args.vpc_id)
        network_name = short_name(net.self_link) or args.vpc_id
        result["network_id"] = network_name

        # Real zones for the az field (cycled, one per subnet entry).
        zones = region_zones(project, args.region)

        subnets = list_subnetworks_for_network(project, args.region, args.vpc_id)
        subnet_zones = cycle_zones(zones, len(subnets))

        subnet_entries: list[dict[str, Any]] = []
        for idx, sub in enumerate(subnets):
            cidr = sub.ip_cidr_range
            subnet_entries.append(
                {
                    "subnet_id": short_name(sub.self_link) or sub.name,
                    "cidr": cidr,
                    "az": subnet_zones[idx] if idx < len(subnet_zones) else "",
                    # External IPs are per-NIC at launch, not a subnet
                    # attribute — false; reconcile via auto_assign_ip_mode
                    # =instance.
                    "auto_assign_public_ip": False,
                    "available_ips": usable_ip_count(cidr),
                }
            )
        result["subnets"] = subnet_entries

        # Top-level cidr: the create-time aggregate forwarded from
        # create_network (the network owns no CIDR). Each subnet was carved
        # from this aggregate, so subnet_of holds (VpcIpConfigCheck) without
        # re-deriving a narrower supernet from the live subnet ranges.
        result["cidr"] = args.cidr

        # DHCP options — synthesized from the metadata-server resolver; the
        # helper OMITS domain_name (string-typed schema rejects null).
        result["dhcp_options"] = dhcp_options_payload(network_name)

        result["success"] = True

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
