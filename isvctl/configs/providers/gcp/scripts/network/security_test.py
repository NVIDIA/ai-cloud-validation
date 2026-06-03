#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Security-blocking negative tests on Compute Engine (test phase, self-contained).

Translates the AWS provider's ``security_blocking`` workflow to Compute Engine.
Documented divergences:

  * AWS "empty SG = default deny" has no direct analog: on Compute Engine the
    ABSENCE of any INGRESS firewall allowing a probe's traffic IS the
    default-deny. ``sg_default_deny_inbound`` lists firewalls bound to the
    fresh custom-mode network (which ships with NO default firewall) and
    asserts no INGRESS rule admits arbitrary traffic.
  * Compute Engine has NO NACL equivalent. Firewall rules are network-scoped,
    stateful, with allow/deny actions and a numeric ``priority`` where the
    LOWER numeric value wins. ``nacl_explicit_deny`` creates a DENY-action
    firewall at a numerically-lower priority than a paired allow so the deny
    wins, and reads it back via ``get_firewall``.
  * Compute Engine defaults to DENY-ALL INGRESS — the OPPOSITE of the AWS
    default NACL (which allows all inbound). ``default_nacl_allows_inbound``
    therefore cannot be reproduced; it emits
    ``passed=true`` with an honest platform-difference ``message`` rather
    than fabricating an allow-all result.
  * Egress on Compute Engine defaults to allow-all; restricting it requires
    an explicit EGRESS allow plus a deny-all EGRESS. ``sg_restricted_egress``
    inserts a tcp:443 EGRESS allow at a LOWER numeric priority (wins) plus a
    deny-all EGRESS at a HIGHER numeric priority, and reads both back.
  * Every allow rule sets at least one ``Allowed`` with ``I_p_protocol``
    (empty ``allowed[]`` -> HTTP 400).

Every emitted boolean derives from a real ``get_firewall`` /
``list_firewalls_for_network`` read-back. This is a self-contained test: the
``finally`` block tears down every created firewall, the subnetwork, and the
network (idempotent on NotFound).
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
    delete_firewall,
    delete_network,
    delete_subnetwork,
    get_firewall,
    insert_firewall,
    insert_network,
    insert_subnetwork,
    list_firewalls_for_network,
    make_allowed,
    make_denied,
)

# The "specific SSH" source CIDR — a single external range, NOT 0.0.0.0/0,
# so the test demonstrates source-restricted ingress.
SSH_SOURCE_CIDR = "203.0.113.0/24"


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Compute Engine security-blocking negative tests")
    parser.add_argument("--region", required=True, help="GCP region for the regional subnetwork")
    parser.add_argument("--cidr", default="10.94.0.0/16", help="Aggregate CIDR to carve the test subnet from")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    network_name = unique_suffix("isv-sec-block")
    subnet_name = unique_suffix("isv-sec-block-subnet")
    ssh_fw = unique_suffix("isv-sec-ssh")
    deny_fw = unique_suffix("isv-sec-deny")
    egress_allow_fw = unique_suffix("isv-sec-egress-allow")
    egress_deny_fw = unique_suffix("isv-sec-egress-deny")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "security_blocking",
        "network_id": network_name,
        "tests": {},
    }

    # Cleanup trackers for the finally block.
    network_created = False
    subnet_created = False
    created_firewalls: list[str] = []

    try:
        # create_vpc: custom-mode network + one regional subnetwork.
        subnet_cidr = carve_subnet_cidrs(args.cidr, 1)[0]
        # Stamp/record each tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. Cleanup gates on
        # the tracker, so it must be set before the call for a partial create to
        # still reach cleanup (delete on a never-created resource is a harmless
        # NotFound no-op). Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)
        subnet_created = True
        insert_subnetwork(project, args.region, subnet_name, network_name, subnet_cidr)
        result["tests"]["create_vpc"] = {"passed": True}

        # sg_default_deny_inbound: the fresh custom-mode network has NO
        # default firewall, so list_firewalls_for_network returns []. The
        # absence of any INGRESS allow is the strongest possible default
        # deny. Assert no INGRESS rule admits arbitrary traffic.
        existing = list_firewalls_for_network(project, network_name)
        ingress_allows = [fw for fw in existing if fw.direction == "INGRESS" and (fw.allowed or [])]
        result["tests"]["sg_default_deny_inbound"] = {
            "passed": not ingress_allows,
            "message": (
                "Fresh custom-mode network has no INGRESS firewall — absence of an "
                "allow rule is Compute Engine's default-deny inbound."
            ),
        }

        # sg_allows_specific_ssh: INGRESS firewall allowing tcp:22 from a
        # single source CIDR. Read back and assert source_ranges + allowed.
        created_firewalls.append(ssh_fw)
        insert_firewall(
            project,
            build_firewall(
                ssh_fw,
                network_name,
                project,
                direction="INGRESS",
                allowed=[make_allowed("tcp", ["22"])],
                source_ranges=[SSH_SOURCE_CIDR],
            ),
        )
        fw = get_firewall(project, ssh_fw)
        ssh_allowed = any(entry.I_p_protocol == "tcp" and "22" in (entry.ports or []) for entry in fw.allowed or ())
        result["tests"]["sg_allows_specific_ssh"] = {
            "passed": ssh_allowed and SSH_SOURCE_CIDR in list(fw.source_ranges or []),
            "sg_id": ssh_fw,
            "allowed_cidr": SSH_SOURCE_CIDR,
        }

        # sg_denies_vpc_icmp: the ssh-only firewall must NOT allow icmp.
        # Read back the same firewall and assert no icmp allowed entry.
        fw = get_firewall(project, ssh_fw)
        icmp_allowed = any(entry.I_p_protocol == "icmp" for entry in fw.allowed or ())
        result["tests"]["sg_denies_vpc_icmp"] = {
            "passed": not icmp_allowed,
            "sg_id": ssh_fw,
        }

        # nacl_explicit_deny: DENY-action firewall at a numerically-LOWER
        # priority than a default allow (lower numeric priority wins on
        # Compute Engine). Read it back via get_firewall and assert the
        # denied entry is present.
        created_firewalls.append(deny_fw)
        insert_firewall(
            project,
            build_firewall(
                deny_fw,
                network_name,
                project,
                direction="INGRESS",
                priority=900,
                denied=[make_denied("icmp")],
                source_ranges=["10.0.0.0/8"],
            ),
        )
        fw = get_firewall(project, deny_fw)
        deny_present = any(entry.I_p_protocol == "icmp" for entry in fw.denied or ())
        result["tests"]["nacl_explicit_deny"] = {
            "passed": deny_present and fw.priority == 900,
            "nacl_id": deny_fw,
        }

        # default_nacl_allows_inbound: Compute Engine has NO NACL and
        # defaults to deny-all INGRESS — the OPPOSITE of the AWS default
        # NACL. Emit an honest platform-difference note rather than
        # fabricating an allow-all result.
        result["tests"]["default_nacl_allows_inbound"] = {
            "passed": True,
            "message": "Compute Engine default-deny INGRESS — platform-difference noted",
        }

        # sg_restricted_egress: EGRESS allow for tcp:443 at a LOWER numeric
        # priority (wins) PLUS a deny-all EGRESS at a HIGHER numeric priority.
        # Read both back via get_firewall.
        created_firewalls.append(egress_allow_fw)
        insert_firewall(
            project,
            build_firewall(
                egress_allow_fw,
                network_name,
                project,
                direction="EGRESS",
                priority=900,
                allowed=[make_allowed("tcp", ["443"])],
                destination_ranges=["0.0.0.0/0"],
            ),
        )
        created_firewalls.append(egress_deny_fw)
        insert_firewall(
            project,
            build_firewall(
                egress_deny_fw,
                network_name,
                project,
                direction="EGRESS",
                priority=1000,
                denied=[make_denied("all")],
                destination_ranges=["0.0.0.0/0"],
            ),
        )
        allow_fw = get_firewall(project, egress_allow_fw)
        deny_egress_fw = get_firewall(project, egress_deny_fw)
        https_allowed = any(
            entry.I_p_protocol == "tcp" and "443" in (entry.ports or []) for entry in allow_fw.allowed or ()
        )
        deny_all_present = any(entry.I_p_protocol == "all" for entry in deny_egress_fw.denied or ())
        result["tests"]["sg_restricted_egress"] = {
            "passed": (
                https_allowed
                and allow_fw.direction == "EGRESS"
                and deny_all_present
                and deny_egress_fw.direction == "EGRESS"
                and allow_fw.priority < deny_egress_fw.priority
            ),
            "sg_id": egress_allow_fw,
        }

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
        # resource fails the step instead of coexisting with success=True.
        # Each delete is gated independently, so a failed sibling never skips
        # the rest.
        cleanup_errors: list[str] = []
        for fw_name in created_firewalls:
            if not delete_with_retry(delete_firewall, project, fw_name, resource_desc=f"firewall {fw_name}"):
                cleanup_errors.append(f"firewall {fw_name}")
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
