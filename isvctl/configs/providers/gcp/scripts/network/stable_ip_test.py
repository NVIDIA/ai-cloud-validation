#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Test private-IP stability across stop/start on Compute Engine (step ``stable_ip_test``).

Translates the AWS provider's ``stable_ip_test`` workflow to Compute Engine.
Self-contained: creates its OWN network + subnet + SSH firewall + one VM,
records the internal IP, stops + starts the VM, then asserts the internal
IP is unchanged. Tears everything down in ``finally``.

Documented divergences from the AWS provider:

  * Compute Engine instance status is ``TERMINATED`` (canonical 'stopped')
    when stopped and ``RUNNING`` (canonical 'running') when started — every
    state check flows through ``canonical_state`` via ``poll_instance_state``.
  * Lifecycle ops (``instances.stop`` / ``start``) are async and zone-bound;
    we wrap the sync+wait pair in the in-zone retry-with-backoff envelope
    (``retry_zonal_lifecycle_op``) and then poll for the canonical state.
  * The primary INTERNAL IPv4 (``networkInterfaces[0].networkIP``) persists
    across stop/start by default — no SSH is needed to verify ``ip_unchanged``.
    (Ephemeral EXTERNAL IPs are released on stop, but the contract tests the
    private IP, which is stable.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    delete_local_keypair,
    first_internal_ip,
    generate_ssh_keypair,
    get_instance,
    narrow_region_to_zone,
    poll_instance_state,
    read_ssh_pubkey,
    resolve_project,
    retry_zonal_lifecycle_op,
    unique_suffix,
)
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    DEFAULT_SSH_USER,
    ISV_NETWORK_TAG,
    build_firewall,
    build_probe_instance,
    carve_subnet_cidrs,
    delete_firewall,
    delete_instance,
    delete_network,
    delete_subnetwork,
    insert_firewall,
    insert_instance,
    insert_network,
    insert_subnetwork,
    make_allowed,
)
from google.cloud import compute_v1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test private-IP stability across stop/start")
    parser.add_argument("--region", required=True, help="GCP region (narrowed to <region>-a if no --zone)")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region narrowing)")
    parser.add_argument("--cidr", default="10.91.0.0/16", help="Aggregate CIDR to carve the test subnet from")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)
    region = zone.rsplit("-", 1)[0]
    ssh_user = DEFAULT_SSH_USER

    network_name = unique_suffix("isv-stable-net")
    subnet_name = unique_suffix("isv-stable-subnet")
    fw_name = unique_suffix("isv-stable-fw")
    instance_name = unique_suffix("isv-stable-vm")
    key_name = unique_suffix("isv-stable-key")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "stable_ip",
        "tests": {},
    }

    network_created = False
    subnet_created = False
    fw_created = False
    instance_created = False
    key_priv: str | None = None
    key_created = False

    try:
        key_priv, key_created = generate_ssh_keypair(key_name)
        ssh_pubkey = read_ssh_pubkey(key_priv)

        # Setup: custom-mode network + subnet + SSH firewall (tag-scoped).
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

        # create_instance — launch ONE VM. Stamp the cleanup tracker BEFORE
        # waiting on the async insert.
        inst_resource = build_probe_instance(
            project=project,
            zone=zone,
            name=instance_name,
            network_name=network_name,
            subnet_name=subnet_name,
            ssh_user=ssh_user,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[ISV_NETWORK_TAG],
        )
        instance_created = True
        insert_instance(project, zone, inst_resource)
        poll_instance_state(project, zone, instance_name, target_canonical="running", timeout=300)
        result["tests"]["create_instance"] = {"passed": True, "instance_id": instance_name}

        # record_ip — read the primary internal IPv4.
        inst = get_instance(project, zone, instance_name)
        private_ip = first_internal_ip(inst)
        if not private_ip:
            raise RuntimeError("instance has no internal IP after RUNNING")
        result["tests"]["record_ip"] = {"passed": True, "private_ip": private_ip}

        # stop_instance — async stop, wait + poll canonical 'stopped'.
        client = compute_v1.InstancesClient()
        retry_zonal_lifecycle_op(
            lambda: client.stop(project=project, zone=zone, instance=instance_name),
            project,
            zone,
            resource_desc=f"stop {instance_name}",
        )
        stopped_state = poll_instance_state(project, zone, instance_name, target_canonical="stopped", timeout=300)
        result["tests"]["stop_instance"] = {"passed": stopped_state == "stopped"}

        # start_instance — async start, wait + poll canonical 'running'.
        retry_zonal_lifecycle_op(
            lambda: client.start(project=project, zone=zone, instance=instance_name),
            project,
            zone,
            resource_desc=f"start {instance_name}",
        )
        running_state = poll_instance_state(project, zone, instance_name, target_canonical="running", timeout=300)
        result["tests"]["start_instance"] = {"passed": running_state == "running"}

        # ip_unchanged — re-read internal IP, assert == recorded.
        inst_after = get_instance(project, zone, instance_name)
        ip_after = first_internal_ip(inst_after)
        result["tests"]["ip_unchanged"] = {
            "passed": ip_after == private_ip and ip_after is not None,
            "ip_before": private_ip,
            "ip_after": ip_after,
        }

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # delete_with_retry never raises and returns False only on exhausted
        # retries — capture every cloud-delete bool so a leaked resource fails
        # the step instead of coexisting with success=True. Each delete is
        # gated independently, so a failed sibling never skips the rest.
        cleanup_errors: list[str] = []
        if instance_created and not delete_with_retry(
            delete_instance, project, zone, instance_name, resource_desc=f"instance {instance_name}"
        ):
            cleanup_errors.append(f"instance {instance_name}")
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
