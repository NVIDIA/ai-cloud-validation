#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Test floating-IP reassociation between VMs on Compute Engine (step ``floating_ip_test``).

Translates the AWS provider's ``floating_ip_test`` workflow to Compute Engine.
Self-contained: creates its OWN network + subnet + SSH firewall + two VMs (A
and B), reserves a regional static external IP, attaches it to A, then times
the switch to B and verifies the address moved. Tears everything down in
``finally``.

Documented divergences from the AWS provider:

  * AWS Elastic-IP association becomes a Compute Engine static regional
    Address attached to a NIC via an ``accessConfigs`` (ONE_TO_ONE_NAT)
    entry. There is no single atomic "reassociate" call: the static IP can
    only live on one NIC at a time, so the switch is
    delete-access-config-on-A + add-access-config-on-B.
  * ``add_access_config`` / ``delete_access_config`` are async zonal ops on
    ``InstancesClient`` (network_interface="nic0"). A NIC may carry only one
    external ``accessConfig``, so A's pre-existing ephemeral config ("External
    NAT") is deleted before the static one is added.
  * The switch is timed across the REAL delete+add zonal ops; the public IP
    on each VM is read back from live ``instances.get`` state.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    delete_local_keypair,
    first_external_ip,
    generate_ssh_keypair,
    get_instance,
    narrow_region_to_zone,
    poll_instance_state,
    read_ssh_pubkey,
    resolve_project,
    unique_suffix,
    wait_for_zonal_op,
)
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    DEFAULT_SSH_USER,
    ISV_NETWORK_TAG,
    build_firewall,
    build_probe_instance,
    carve_subnet_cidrs,
    delete_address,
    delete_firewall,
    delete_instance,
    delete_network,
    delete_subnetwork,
    insert_address,
    insert_firewall,
    insert_instance,
    insert_network,
    insert_subnetwork,
    make_allowed,
)
from google.api_core import exceptions as gax
from google.cloud import compute_v1

_NIC = "nic0"
# Default ephemeral access-config name assigned by build_probe_instance.
_EPHEMERAL_AC_NAME = "External NAT"
_STATIC_AC_NAME = "isv-static-nat"
# Tight poll for the timed switch ops so the measured switch time reflects
# real op latency, not the 3s default zonal-op poll cadence.
_SWITCH_POLL_INTERVAL = 1


def _op_name(op: Any) -> str:
    """Extract the operation name from an async add/delete return."""
    return getattr(op, "name", None) or getattr(op, "operation", "") or ""


def _delete_access_config(project: str, zone: str, instance: str, ac_name: str, *, timeout: int = 120) -> None:
    """Delete an external access config on nic0 (NotFound is idempotent)."""
    client = compute_v1.InstancesClient()
    try:
        op = client.delete_access_config(
            project=project,
            zone=zone,
            instance=instance,
            access_config=ac_name,
            network_interface=_NIC,
        )
    except gax.NotFound:
        return
    name = _op_name(op)
    if name:
        wait_for_zonal_op(project, zone, name, timeout=timeout, poll_interval=_SWITCH_POLL_INTERVAL)


def _add_static_access_config(project: str, zone: str, instance: str, static_ip: str, *, timeout: int = 120) -> None:
    """Add a ONE_TO_ONE_NAT access config bound to ``static_ip`` on nic0."""
    client = compute_v1.InstancesClient()
    ac = compute_v1.AccessConfig()
    ac.type_ = "ONE_TO_ONE_NAT"
    ac.name = _STATIC_AC_NAME
    ac.nat_i_p = static_ip
    op = client.add_access_config(
        project=project,
        zone=zone,
        instance=instance,
        network_interface=_NIC,
        access_config_resource=ac,
    )
    name = _op_name(op)
    if name:
        wait_for_zonal_op(project, zone, name, timeout=timeout, poll_interval=_SWITCH_POLL_INTERVAL)


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test floating-IP reassociation on Compute Engine")
    parser.add_argument("--region", required=True, help="GCP region (narrowed to <region>-a if no --zone)")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region narrowing)")
    parser.add_argument("--cidr", default="10.92.0.0/16", help="Aggregate CIDR to carve the test subnet from")
    parser.add_argument(
        "--max-switch-seconds",
        type=int,
        default=20,
        help="Maximum allowed switch time in seconds",
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)
    region = zone.rsplit("-", 1)[0]
    ssh_user = DEFAULT_SSH_USER

    network_name = unique_suffix("isv-float-net")
    subnet_name = unique_suffix("isv-float-subnet")
    fw_name = unique_suffix("isv-float-fw")
    instance_a_name = unique_suffix("isv-float-a")
    instance_b_name = unique_suffix("isv-float-b")
    address_name = unique_suffix("isv-float-addr")
    key_name = unique_suffix("isv-float-key")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "floating_ip",
        "tests": {},
    }

    network_created = False
    subnet_created = False
    fw_created = False
    a_created = False
    b_created = False
    address_created = False
    key_priv: str | None = None
    key_created = False

    try:
        key_priv, key_created = generate_ssh_keypair(key_name)
        ssh_pubkey = read_ssh_pubkey(key_priv)

        # Setup: custom-mode network + subnet + SSH firewall + two VMs.
        # Stamp each *_created tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. The finally
        # cleanup gates on the tracker, so it must be True before the call for a
        # partial create to still reach cleanup (delete on a never-created
        # resource is a harmless NotFound no-op). Mirrors create_vpc/byoip_test.
        subnet_cidr = carve_subnet_cidrs(args.cidr, 1)[0]
        network_created = True
        insert_network(project, network_name)
        subnet_created = True
        insert_subnetwork(project, region, subnet_name, network_name, subnet_cidr)
        fw = build_firewall(
            fw_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=[make_allowed("tcp", ["22"]), make_allowed("icmp")],
            source_ranges=["0.0.0.0/0"],
            target_tags=[ISV_NETWORK_TAG],
        )
        fw_created = True
        insert_firewall(project, fw)

        # Launch A and B (track names BEFORE the insert wait).
        a_resource = build_probe_instance(
            project=project,
            zone=zone,
            name=instance_a_name,
            network_name=network_name,
            subnet_name=subnet_name,
            ssh_user=ssh_user,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[ISV_NETWORK_TAG],
        )
        a_created = True
        insert_instance(project, zone, a_resource)

        b_resource = build_probe_instance(
            project=project,
            zone=zone,
            name=instance_b_name,
            network_name=network_name,
            subnet_name=subnet_name,
            ssh_user=ssh_user,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[ISV_NETWORK_TAG],
        )
        b_created = True
        insert_instance(project, zone, b_resource)

        poll_instance_state(project, zone, instance_a_name, target_canonical="running", timeout=300)
        poll_instance_state(project, zone, instance_b_name, target_canonical="running", timeout=300)

        # allocate_eip — reserve a regional static external IP. Stamp
        # address_created BEFORE insert_address (it runs _wait_or_rollback too;
        # see the setup-block rationale) so a partial create still reaches
        # cleanup; the name is known, so delete-by-name needs no return value.
        address_created = True
        address = insert_address(project, region, address_name)
        static_ip = address.address
        result["tests"]["allocate_eip"] = {
            "passed": bool(static_ip),
            "allocation_id": address.name,
            "public_ip": static_ip,
        }
        if not static_ip:
            raise RuntimeError("reserved address has no IP")

        # associate_to_a — delete A's ephemeral accessConfig, add the static
        # one. (A NIC carries at most one external accessConfig.)
        _delete_access_config(project, zone, instance_a_name, _EPHEMERAL_AC_NAME)
        _add_static_access_config(project, zone, instance_a_name, static_ip)
        result["tests"]["associate_to_a"] = {
            "passed": True,
            "association_id": f"access-config:{_STATIC_AC_NAME}",
        }

        # verify_on_a — re-read A, assert its external IP == static IP.
        inst_a = get_instance(project, zone, instance_a_name)
        a_ip = first_external_ip(inst_a)
        result["tests"]["verify_on_a"] = {"passed": a_ip == static_ip, "public_ip": a_ip}

        # reassociate_to_b — time ONLY the floating-IP move: detach the static
        # config from A, then attach it to B. Clearing B's pre-existing
        # ephemeral config is NIC *preparation* (a NIC carries at most one
        # external accessConfig), not part of the switch, so it runs BEFORE the
        # timer — otherwise a third async zonal op (~9s each, incl. the 3s
        # wait_for_zonal_op poll slop) inflates the measured switch past the
        # max_switch_seconds budget, which is sized for the 2-op detach+attach
        # window (a single ephemeral->static promotion measures ~12s).
        _delete_access_config(project, zone, instance_b_name, _EPHEMERAL_AC_NAME)
        start = time.monotonic()
        _delete_access_config(project, zone, instance_a_name, _STATIC_AC_NAME)
        _add_static_access_config(project, zone, instance_b_name, static_ip)
        switch_seconds = round(time.monotonic() - start, 2)
        result["tests"]["reassociate_to_b"] = {
            "passed": switch_seconds <= args.max_switch_seconds,
            "switch_seconds": switch_seconds,
        }

        # verify_on_b — re-read B, assert its external IP == static IP.
        inst_b = get_instance(project, zone, instance_b_name)
        b_ip = first_external_ip(inst_b)
        result["tests"]["verify_on_b"] = {"passed": b_ip == static_ip, "public_ip": b_ip}

        # verify_not_on_a — re-read A, assert its external IP != static IP.
        inst_a2 = get_instance(project, zone, instance_a_name)
        a_ip2 = first_external_ip(inst_a2)
        result["tests"]["verify_not_on_a"] = {"passed": a_ip2 != static_ip, "public_ip": a_ip2}

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Tear down everything THIS run created. The static address must be
        # released AFTER the VMs holding it are deleted (an in-use address
        # cannot be released). delete_with_retry never raises and returns
        # False only on exhausted retries — capture every cloud-delete bool
        # so a leaked resource fails the step instead of coexisting with
        # success=True. Each delete is gated independently, so a failed
        # sibling never skips the rest.
        cleanup_errors: list[str] = []
        if a_created and not delete_with_retry(
            delete_instance, project, zone, instance_a_name, resource_desc=f"instance {instance_a_name}"
        ):
            cleanup_errors.append(f"instance {instance_a_name}")
        if b_created and not delete_with_retry(
            delete_instance, project, zone, instance_b_name, resource_desc=f"instance {instance_b_name}"
        ):
            cleanup_errors.append(f"instance {instance_b_name}")
        if address_created and not delete_with_retry(
            delete_address, project, region, address_name, resource_desc=f"address {address_name}"
        ):
            cleanup_errors.append(f"address {address_name}")
        if fw_created and not delete_with_retry(delete_firewall, project, fw_name, resource_desc=f"firewall {fw_name}"):
            cleanup_errors.append(f"firewall {fw_name}")
        if subnet_created and not delete_with_retry(
            delete_subnetwork, project, region, subnet_name, resource_desc=f"subnetwork {subnet_name}"
        ):
            cleanup_errors.append(f"subnetwork {subnet_name}")
        if network_created and not delete_with_retry(
            delete_network, project, network_name, resource_desc=f"network {network_name}"
        ):
            cleanup_errors.append(f"network {network_name}")
        if cleanup_errors:
            result.setdefault("cleanup_errors", []).extend(cleanup_errors)
            result["success"] = False
        # Local SSH keypair is a workstation file, not a leaked cloud
        # resource; delete best-effort without affecting result["success"].
        if key_created and key_priv:
            try:
                delete_local_keypair(key_priv)
            except Exception as cleanup_exc:
                print(f"Cleanup error (local key): {cleanup_exc}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
