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

"""Security-group (firewall) CRUD test on Compute Engine (test phase, self-contained).

Translates the AWS provider's ``sg_crud`` workflow to Compute Engine.
Documented divergences:

  * AWS security groups are VPC-scoped objects whose inbound/outbound rule
    sets can be authorized/revoked down to EMPTY. Compute Engine firewalls
    are project-scoped, network-bound, UNIDIRECTIONAL (INGRESS / EGRESS),
    and CANNOT have an empty ``allowed[]`` (the API returns HTTP 400). The
    rule set lives on the firewall proto itself and is mutated via
    ``FirewallsClient.patch``.
  * Because a firewall cannot be emptied, ``update_sg_remove_rule`` cannot
    be implemented as "revoke the last inbound rule". We use the TWO-firewall
    pattern: a primary ``firewall_main`` drives the
    create/read/add/modify lifecycle, and a secondary ``firewall_aux`` exists
    solely so ``update_sg_remove_rule`` has a firewall to DELETE (asserting
    NotFound on read-back) without leaving the parent test with an illegal
    empty allow set. ``delete_sg`` then deletes ``firewall_main`` and
    ``verify_deleted`` asserts NotFound on it.
  * Firewalls are unidirectional, so ``read_sg`` emits ``inbound_rule_count``
    from ``len(allowed)`` and ``outbound_rule_count`` as 0 (this INGRESS
    firewall has no egress rule set).
  * Every allow rule sets at least one ``Allowed`` with ``I_p_protocol``
    (empty ``allowed[]`` -> HTTP 400).

Every emitted boolean derives from a real read-back (``get_firewall`` after
each mutation, or a ``NotFound`` from ``get_firewall`` after each delete).
This is a self-contained test: the ``finally`` block tears down both
firewalls, the subnetwork, and the network (idempotent on NotFound).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, short_name, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    build_firewall,
    carve_subnet_cidrs,
    delete_firewall,
    delete_network,
    delete_subnetwork,
    get_firewall,
    insert_firewall,
    insert_network,
    insert_subnetwork,
    make_allowed,
    patch_firewall,
)
from google.api_core import exceptions as gax

# Base tcp port the firewall CRUD lifecycle is built around. This is a pure
# firewall-object CRUD test: it never launches a VM and never opens a
# connection, so the port is an arbitrary placeholder that only needs to be
# present/added/removed on read-back. It deliberately avoids the admin ports
# tcp/22 (SSH) and tcp/3389 (RDP) — whose ingress is governed by
# NETWORK_FIREWALL_TRUST_IP and must never open to 0.0.0.0/0 — so the rule is
# honest about not being an SSH/RDP path. It must differ from the add/modify
# ports (8080 / 9090) exercised below.
BASE_PORT = "8000"


def _allowed_ports(fw: Any, protocol: str) -> list[str]:
    """Return the ports configured for ``protocol`` in a firewall's allowed[]."""
    for entry in fw.allowed or ():
        if entry.I_p_protocol == protocol:
            return list(entry.ports or [])
    return []


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Compute Engine firewall (security group) CRUD test")
    parser.add_argument("--region", required=True, help="GCP region for the regional subnetwork")
    parser.add_argument("--cidr", default="10.95.0.0/16", help="Aggregate CIDR to carve the test subnet from")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    network_name = unique_suffix("isv-sg-crud")
    subnet_name = unique_suffix("isv-sg-crud-subnet")
    firewall_main = unique_suffix("isv-sg-crud-main")
    firewall_aux = unique_suffix("isv-sg-crud-aux")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sg_crud",
        "network_id": network_name,
        "tests": {},
    }

    # Cleanup trackers for the finally block.
    network_created = False
    subnet_created = False
    main_created = False
    aux_created = False

    try:
        # create_vpc: custom-mode network + one regional subnetwork. The
        # network owns no CIDR; --cidr is the aggregate the subnet carves from.
        subnet_cidr = carve_subnet_cidrs(args.cidr, 1)[0]
        # Stamp each *_created tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. The finally
        # cleanup gates on the tracker, so it must be True before the call for a
        # partial create to still reach cleanup (delete on a never-created
        # resource is a harmless NotFound no-op). Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)
        subnet_created = True
        insert_subnetwork(project, args.region, subnet_name, network_name, subnet_cidr)
        result["tests"]["create_vpc"] = {"passed": True, "vpc_id": network_name}

        # create_sg: insert TWO INGRESS firewalls, each with >=1 allowed
        # entry (empty allowed[] -> HTTP 400). firewall_main drives the
        # lifecycle; firewall_aux exists so update_sg_remove_rule has a
        # firewall to delete. sg_id is firewall_main's name.
        main_created = True
        insert_firewall(
            project,
            build_firewall(
                firewall_main,
                network_name,
                project,
                direction="INGRESS",
                allowed=[make_allowed("tcp", [BASE_PORT])],
                source_ranges=["0.0.0.0/0"],
            ),
        )
        aux_created = True
        insert_firewall(
            project,
            build_firewall(
                firewall_aux,
                network_name,
                project,
                direction="INGRESS",
                allowed=[make_allowed("tcp", ["443"])],
                source_ranges=["0.0.0.0/0"],
            ),
        )
        result["tests"]["create_sg"] = {"passed": True, "sg_id": firewall_main}

        # read_sg: get firewall_main and read back its attributes.
        fw = get_firewall(project, firewall_main)
        result["tests"]["read_sg"] = {
            "passed": fw.name == firewall_main,
            "name": fw.name,
            "description": fw.description,
            "vpc_id": short_name(fw.network),
            "inbound_rule_count": len(fw.allowed or []),
            "outbound_rule_count": 0,
        }

        # update_sg_add_rule: patch firewall_main appending tcp:8080 to
        # allowed[], then read back to confirm the new port is present.
        patch_firewall(
            project,
            firewall_main,
            build_firewall(
                firewall_main,
                network_name,
                project,
                direction="INGRESS",
                allowed=[make_allowed("tcp", [BASE_PORT, "8080"])],
                source_ranges=["0.0.0.0/0"],
            ),
        )
        fw = get_firewall(project, firewall_main)
        tcp_ports = _allowed_ports(fw, "tcp")
        result["tests"]["update_sg_add_rule"] = {
            "passed": "8080" in tcp_ports,
            "rule_added": "tcp/8080",
        }

        # update_sg_modify_rule: patch firewall_main swapping the added port
        # 8080 -> 9090, then read back to confirm 9090 present and 8080 gone.
        patch_firewall(
            project,
            firewall_main,
            build_firewall(
                firewall_main,
                network_name,
                project,
                direction="INGRESS",
                allowed=[make_allowed("tcp", [BASE_PORT, "9090"])],
                source_ranges=["0.0.0.0/0"],
            ),
        )
        fw = get_firewall(project, firewall_main)
        tcp_ports = _allowed_ports(fw, "tcp")
        result["tests"]["update_sg_modify_rule"] = {
            "passed": "9090" in tcp_ports and "8080" not in tcp_ports,
            "rule_before": "tcp/8080",
            "rule_after": "tcp/9090",
        }

        # update_sg_remove_rule: delete firewall_aux, then assert get raises
        # NotFound. (A firewall cannot be emptied to an empty allowed[], so
        # "remove the last rule" is honestly modeled as deleting the firewall.)
        delete_firewall(project, firewall_aux)
        aux_created = False
        removed = False
        try:
            get_firewall(project, firewall_aux)
        except gax.NotFound:
            removed = True
        result["tests"]["update_sg_remove_rule"] = {"passed": removed}

        # delete_sg: delete firewall_main.
        delete_firewall(project, firewall_main)
        main_created = False
        result["tests"]["delete_sg"] = {"passed": True}

        # verify_deleted: get firewall_main must raise NotFound.
        deleted = False
        try:
            get_firewall(project, firewall_main)
        except gax.NotFound:
            deleted = True
        result["tests"]["verify_deleted"] = {"passed": deleted}

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Self-contained test: tear down everything it created. Delete
        # firewalls + subnetwork before the network (dependency order).
        # NotFound is idempotent. delete_with_retry never raises and returns
        # False only on exhausted retries — capture every bool so a leaked
        # resource drags result["success"] to False instead of coexisting
        # with a green step (the helper's bool is a hard contract, not
        # best-effort). Each delete is independently gated, so a failed
        # sibling cleanup never skips the remaining deletes.
        cleanup_errors: list[str] = []
        if aux_created and not delete_with_retry(
            delete_firewall, project, firewall_aux, resource_desc=f"firewall {firewall_aux}"
        ):
            cleanup_errors.append(f"firewall {firewall_aux}")
        if main_created and not delete_with_retry(
            delete_firewall, project, firewall_main, resource_desc=f"firewall {firewall_main}"
        ):
            cleanup_errors.append(f"firewall {firewall_main}")
        if subnet_created and not delete_with_retry(
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
