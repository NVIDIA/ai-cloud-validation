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

"""Bring-Your-Own-IP test for Compute Engine (test phase, step ``byoip_test``).

Translates the AWS provider's ``byoip_test`` workflow to Compute Engine. The
five named subtests BYOIP requires (``custom_cidr_create``,
``custom_cidr_verify``, ``standard_cidr_create``, ``no_conflict``,
``custom_cidr_subnet``) are preserved by JSON key.

Documented divergences:

  * Networks own NO CIDR — on AWS a VPC accepts a custom CIDR via CreateVpc,
    but a Compute Engine custom-mode network has no CIDR field at all. The
    custom / standard CIDRs are applied to SUBNETWORKS via ``ipCidrRange``.
    ``custom_cidr_create`` therefore creates a network AND a subnetwork in
    the requested CIDR, emitting ``vpc_id`` as the Network.name and ``cidr``
    as the Subnetwork.ipCidrRange read back from the live subnet.
  * ``Subnetwork.state`` is EMPTY for a freshly-created custom-mode subnet
    even after the regional insert op reports DONE (documented GCE quirk).
    The op reaching DONE IS the readiness signal — ``custom_cidr_verify``
    emits ``state: "READY"`` rather than propagating the empty proto field
    as a false negative.
  * The ``no_conflict`` assertion compares the two SUBNETWORK CIDRs (the
    network has none) using :mod:`ipaddress` overlap detection.

The test creates and deletes its OWN two networks (not the shared setup
network). The ``finally`` block deletes both subnetworks first, then both
networks, gated on the per-resource created flags so a mid-creation failure
does not leak.
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
    delete_network,
    delete_subnetwork,
    get_subnetwork,
    insert_network,
    insert_subnetwork,
    subnet_readiness_state,
)


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test BYOIP / custom-CIDR support on Compute Engine")
    parser.add_argument("--region", required=True, help="GCP region for the regional subnetworks")
    parser.add_argument("--custom-cidr", default="100.64.0.0/24", help="Custom (BYOIP) subnet CIDR")
    parser.add_argument("--standard-cidr", default="10.90.0.0/24", help="Standard RFC1918 subnet CIDR")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    # Compute Engine names ARE the API IDs — run-id-suffix everything so
    # parallel runs don't collide on AlreadyExists.
    custom_network = unique_suffix("isv-byoip-custom")
    custom_subnet = unique_suffix("isv-byoip-custom-subnet")
    standard_network = unique_suffix("isv-byoip-standard")
    standard_subnet = unique_suffix("isv-byoip-standard-subnet")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "byoip_test",
        "tests": {
            "custom_cidr_create": {"passed": False},
            "custom_cidr_verify": {"passed": False},
            "standard_cidr_create": {"passed": False},
            "no_conflict": {"passed": False},
            "custom_cidr_subnet": {"passed": False},
        },
    }

    # Cleanup trackers for the finally block.
    custom_network_created = False
    custom_subnet_created = False
    standard_network_created = False
    standard_subnet_created = False

    try:
        # 1. custom_cidr_create — create the custom-mode network and a
        # subnetwork carrying the custom CIDR (the network has no CIDR).
        # Stamp each created tracker BEFORE its insert: the name is
        # deterministic, so if the insert ack lands but the op-wait and its
        # best-effort rollback both fail (PartialCreateError), the finally
        # block must still see the tracker True to clean the leaked resource.
        # A delete on a never-created resource is a harmless NotFound no-op.
        custom_network_created = True
        insert_network(project, custom_network)
        custom_subnet_created = True
        insert_subnetwork(project, args.region, custom_subnet, custom_network, args.custom_cidr)
        # Read the CIDR back off the live subnet rather than echoing the arg.
        custom_live = get_subnetwork(project, args.region, custom_subnet)
        result["tests"]["custom_cidr_create"] = {
            "passed": True,
            "vpc_id": custom_network,
            "cidr": custom_live.ip_cidr_range,
        }

        # 2. custom_cidr_verify — the regional insert op reaching DONE IS the
        # readiness signal; Subnetwork.state is empty for fresh subnets, so
        # emit "READY" (never the empty proto field as a false negative).
        verify_live = get_subnetwork(project, args.region, custom_subnet)
        result["tests"]["custom_cidr_verify"] = {
            "passed": verify_live.ip_cidr_range == args.custom_cidr,
            "cidr": verify_live.ip_cidr_range,
            "state": subnet_readiness_state(op_done=True),
        }

        # 3. standard_cidr_create — second network + subnetwork with the
        # standard RFC1918 CIDR. Stamp-before-insert (see step 1 rationale)
        # so a partial-create failure still hands the resource to cleanup.
        standard_network_created = True
        insert_network(project, standard_network)
        standard_subnet_created = True
        insert_subnetwork(project, args.region, standard_subnet, standard_network, args.standard_cidr)
        standard_live = get_subnetwork(project, args.region, standard_subnet)
        result["tests"]["standard_cidr_create"] = {
            "passed": True,
            "vpc_id": standard_network,
            "cidr": standard_live.ip_cidr_range,
        }

        # 4. no_conflict — assert the two SUBNETWORK CIDRs do not overlap.
        cidr_a = custom_live.ip_cidr_range
        cidr_b = standard_live.ip_cidr_range
        net_a = ipaddress.ip_network(cidr_a, strict=False)
        net_b = ipaddress.ip_network(cidr_b, strict=False)
        result["tests"]["no_conflict"] = {
            "passed": not net_a.overlaps(net_b),
            "cidr_a": cidr_a,
            "cidr_b": cidr_b,
        }

        # 5. custom_cidr_subnet — confirm the custom-CIDR subnetwork exists in
        # its network (exact tail-match on the bound network self-link).
        subnet_live = get_subnetwork(project, args.region, custom_subnet)
        bound_to_custom = subnet_live.network.rsplit("/", 1)[-1] == custom_network
        result["tests"]["custom_cidr_subnet"] = {
            "passed": bound_to_custom,
            "subnet_id": custom_subnet,
            "subnet_cidr": subnet_live.ip_cidr_range,
        }

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Delete subnetworks first (they block network deletion), then the
        # networks. Gate strictly on the per-resource created flags.
        # delete_with_retry never raises and returns False only on exhausted
        # retries — capture every bool so a leaked resource fails the step
        # instead of coexisting with success=True. Each delete is gated
        # independently, so a failed sibling cleanup never skips the rest.
        cleanup_errors: list[str] = []
        if custom_subnet_created:
            print(f"Cleanup: deleting subnetwork {custom_subnet}", file=sys.stderr)
            if not delete_with_retry(
                delete_subnetwork, project, args.region, custom_subnet, resource_desc=f"subnetwork {custom_subnet}"
            ):
                cleanup_errors.append(f"subnetwork {custom_subnet}")
        if standard_subnet_created:
            print(f"Cleanup: deleting subnetwork {standard_subnet}", file=sys.stderr)
            if not delete_with_retry(
                delete_subnetwork, project, args.region, standard_subnet, resource_desc=f"subnetwork {standard_subnet}"
            ):
                cleanup_errors.append(f"subnetwork {standard_subnet}")
        if custom_network_created:
            print(f"Cleanup: deleting network {custom_network}", file=sys.stderr)
            if not delete_with_retry(
                delete_network, project, custom_network, resource_desc=f"network {custom_network}"
            ):
                cleanup_errors.append(f"network {custom_network}")
        if standard_network_created:
            print(f"Cleanup: deleting network {standard_network}", file=sys.stderr)
            if not delete_with_retry(
                delete_network, project, standard_network, resource_desc=f"network {standard_network}"
            ):
                cleanup_errors.append(f"network {standard_network}")
        if cleanup_errors:
            result.setdefault("cleanup_errors", []).extend(cleanup_errors)
            result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
