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

"""List Compute Engine instances filtered by network.

Translates the AWS oracle's region-scoped ``describe_instances(VPC=...)``
to Compute Engine's zone-scoped reality:

  * ``instances.list`` is zone-scoped; cross-zone listing requires
    ``aggregatedList``.
  * Filter aggregatedList output to zones inside the operator-supplied
    region (oracle parity — AWS describe_instances doesn't leak in
    instances from other regions).
  * Network/VPC match is exact-equality on the trailing path segment of
    the network self-link (per the scope-binding-comparison rule:
    substring / startswith accept supersets).

The validator (``InstanceListCheck``) only reads ``instances`` and
``found_target``; the canonical state translation comes from
``common.compute.canonical_state``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    canonical_state,
    first_external_ip,
    first_internal_ip,
    narrow_region_to_zone,
    resolve_project,
    short_name,
    zone_to_region,
)
from common.errors import handle_gcp_errors
from google.cloud import compute_v1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="List Compute Engine instances by network")
    parser.add_argument("--vpc-id", required=True, help="Network short name")
    parser.add_argument("--instance-id", help="Specific instance to look up")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    effective_zone = args.zone or narrow_region_to_zone(args.region)
    target_region = zone_to_region(effective_zone)
    # Compute Engine aggregatedList keys zones as ``zones/<region>-<letter>``.
    # Use the region prefix so the cross-region instances are filtered out
    # — region-scoped oracle vs zone-scoped target: never silently fall back.
    zone_prefix = f"zones/{target_region}-"

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instances": [],
        "count": 0,
        "total_count": 0,
        "region": args.region,
        "zone": effective_zone,
        "project": project,
    }

    try:
        client = compute_v1.InstancesClient()
        request = compute_v1.AggregatedListInstancesRequest(
            project=project,
            max_results=500,
        )
        for zone_key, scoped in client.aggregated_list(request=request):
            if not zone_key.startswith(zone_prefix):
                continue
            for inst in scoped.instances or []:
                inst_network = ""
                if inst.network_interfaces:
                    inst_network = short_name(inst.network_interfaces[0].network)
                if inst_network != args.vpc_id:
                    continue
                result["instances"].append(
                    {
                        "instance_id": inst.name,
                        "instance_type": short_name(inst.machine_type),
                        "state": canonical_state(inst.status),
                        "public_ip": first_external_ip(inst),
                        "private_ip": first_internal_ip(inst),
                        "vpc_id": inst_network,
                    }
                )

        result["count"] = len(result["instances"])
        result["total_count"] = result["count"]

        if args.instance_id:
            result["target_instance"] = args.instance_id
            result["found_target"] = any(i["instance_id"] == args.instance_id for i in result["instances"])

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
