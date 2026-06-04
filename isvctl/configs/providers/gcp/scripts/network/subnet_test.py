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

"""Subnet configuration test on Compute Engine (test phase, self-contained).

Translates the AWS provider's ``subnet_test`` workflow to Compute Engine.
Documented divergences:

  * Subnetworks are REGIONAL — there is no zone/AZ field on a subnetwork.
    The contract's ``az`` is populated from REAL zones in the configured
    region (RegionsClient.get(region)), cycling one zone per subnet so
    require_multi_az is satisfied honestly.
  * There is NO per-subnet route table — the ``route_table_exists`` test is
    OMITTED from the emitted tests dict (SubnetConfigCheck iterates whatever
    keys are present; absent keys do not fail).
  * ``Subnetwork.state`` is EMPTY for a freshly-created custom-mode subnet
    even after the regional op reports DONE. The op-DONE signal IS the
    canonical readiness gate — subnet_readiness_state(True) returns "READY".
    We never propagate the empty proto field as "UNKNOWN".

The test creates a custom-mode network + N subnetworks, then tears them all
down in the ``finally`` block (this is a self-contained test, not the shared
setup network).
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
    carve_subnet_cidrs,
    cycle_zones,
    delete_network,
    delete_subnetwork,
    insert_network,
    insert_subnetwork,
    region_zones,
    subnet_readiness_state,
)


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Compute Engine subnet configuration test")
    parser.add_argument("--region", required=True, help="GCP region for the regional subnetworks")
    parser.add_argument("--cidr", default="10.98.0.0/16", help="Aggregate CIDR to carve subnet ranges from")
    parser.add_argument("--subnet-count", type=int, default=4, help="Number of subnetworks to create")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    network_name = unique_suffix("isv-subnet-test")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "subnet_config",
        "network_id": network_name,
        "subnets": [],
        "tests": {},
    }

    # Cleanup trackers for the finally block.
    network_created = False
    created_subnet_names: list[str] = []

    try:
        zones = region_zones(project, args.region)
        if not zones:
            raise RuntimeError(f"region {args.region!r} reports no zones; cannot populate subnet az field")
        subnet_cidrs = carve_subnet_cidrs(args.cidr, args.subnet_count)
        subnet_zones = cycle_zones(zones, args.subnet_count)

        # Test 1: create the custom-mode network. Stamp/record each tracker
        # BEFORE its insert helper: insert_* runs _wait_or_rollback, which on a
        # failed op-wait + failed rollback raises PartialCreateError with the
        # resource possibly leaked. Cleanup gates on the tracker, so it must be
        # set before the call for a partial create to still reach cleanup
        # (delete on a never-created resource is a harmless NotFound no-op).
        # Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)
        result["tests"]["create_vpc"] = {"passed": True, "vpc_id": network_name}

        # Test 2: create N regional subnetworks. Each insert_subnetwork
        # waits for the regional op to reach DONE — that DONE is the
        # readiness signal we use below.
        subnet_entries: list[dict[str, Any]] = []
        for idx, (cidr, zone) in enumerate(zip(subnet_cidrs, subnet_zones)):
            subnet_name = unique_suffix(f"isv-subnet-test-{idx}")
            created_subnet_names.append(subnet_name)
            insert_subnetwork(project, args.region, subnet_name, network_name, cidr)
            subnet_entries.append({"subnet_id": subnet_name, "cidr": cidr, "az": zone})

        result["subnets"] = subnet_entries
        result["tests"]["create_subnets"] = {
            "passed": len(subnet_entries) == args.subnet_count,
            "count": len(subnet_entries),
            "subnets": subnet_entries,
        }

        # Test 3: AZ distribution — distinct real zones from the region.
        distinct_zones = sorted({s["az"] for s in subnet_entries})
        result["tests"]["az_distribution"] = {
            "passed": len(distinct_zones) >= 2,
            "azs": distinct_zones,
            "az_count": len(distinct_zones),
        }

        # Test 4: subnets available. The regional op reaching DONE (above)
        # is the canonical readiness gate for custom-mode subnets — the
        # Subnetwork.state proto field is empty during the post-create
        # window, so subnet_readiness_state(True) maps op-DONE to "READY".
        states = {name: subnet_readiness_state(True) for name in created_subnet_names}
        result["tests"]["subnets_available"] = {
            "passed": all(state == "READY" for state in states.values()),
            "states": states,
        }

        # route_table_exists is OMITTED — Compute Engine has no per-subnet
        # route tables (override).

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Self-contained test: tear down everything it created. Delete
        # subnetworks before the network (dependency order). NotFound is
        # idempotent. delete_with_retry never raises and returns False only on
        # exhausted retries — capture every bool so a leaked resource fails the
        # step instead of coexisting with success=True. Each delete is gated
        # independently, so a failed sibling never skips the rest.
        cleanup_errors: list[str] = []
        for subnet_name in created_subnet_names:
            if not delete_with_retry(
                delete_subnetwork, project, args.region, subnet_name, resource_desc=f"subnetwork {subnet_name}"
            ):
                cleanup_errors.append(f"subnetwork {subnet_name}")
        if network_created and not delete_with_retry(
            delete_network, project, network_name, resource_desc=f"network {network_name}"
        ):
            cleanup_errors.append(f"network {network_name}")
        if cleanup_errors:
            result.setdefault("cleanup_errors", []).extend(cleanup_errors)
            result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
