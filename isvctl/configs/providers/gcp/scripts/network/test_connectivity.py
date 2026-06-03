#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Test network connectivity between probe instances in the shared VPC.

Translates the AWS provider's ``test_connectivity`` workflow to Compute
Engine. Documented divergences:

  * No SSM equivalent — remote command execution is SSH. Generate a local
    keypair, push the public key via instance metadata ``ssh-keys``, SSH
    over the external IP, run ping, parse latency. ``wait_for_ssh`` +
    ``wait_for_ssh_stable`` replace SSM agent registration.
  * No SSM/IAM instance-profile role is required — ``iam_profile`` is
    emitted as ``null``. Compute Engine attaches a service account at
    launch but the connectivity probe needs none.
  * VPC validation cross-checks subnetwork.network and firewall.network
    against the supplied network by EXACT tail match (scope-binding
    equality, never substring/startswith), via ``short_name``.
  * The shared ``--sg-id`` firewall (from create_vpc) already allows
    tcp:22 + icmp from 0.0.0.0/0, but it scopes to the whole network with
    no target tags. VM1->VM2 PRIVATE ICMP is allowed by it, but to keep
    the probe self-sufficient we create a small intra-VPC INGRESS firewall
    allowing icmp+tcp from the validated subnets' CIDR ranges, suffixed,
    targeting the probe network tag, and delete it in ``finally``.
  * This step uses the SHARED create_network VPC; it does NOT create its
    own network.

Cleanup discipline: each probe instance name is stamped as a local cleanup
tracker IMMEDIATELY after the insert ack, BEFORE the async wait. Both probe VMs, the intra-VPC firewall, and the local SSH key are
torn down in ``finally`` regardless of outcome.
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
    first_external_ip,
    first_internal_ip,
    generate_ssh_keypair,
    get_instance,
    narrow_region_to_zone,
    poll_instance_state,
    read_ssh_pubkey,
    resolve_project,
    short_name,
    unique_suffix,
    wait_for_public_ip,
)
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    DEFAULT_SSH_USER,
    ISV_NETWORK_TAG,
    build_firewall,
    build_probe_instance,
    delete_firewall,
    delete_instance,
    get_firewall,
    get_subnetwork,
    insert_firewall,
    insert_instance,
    make_allowed,
)
from common.ssh_utils import ssh_run, wait_for_ssh, wait_for_ssh_stable
from google.api_core import exceptions as gax

# Bound the per-attempt cleanup waits so the 3-attempt delete_with_retry
# does not multiply the zonal-op + global-op budgets into the enclosing
# step timeout.
_CLEANUP_INSTANCE_WAIT_S = 180
_CLEANUP_FIREWALL_WAIT_S = 120


def validate_vpc_resources(
    project: str,
    region: str,
    vpc_id: str,
    subnet_ids: list[str],
    sg_id: str,
) -> dict[str, Any]:
    """Validate that subnetworks and the firewall belong to ``vpc_id``.

    Scope-binding equality: ``short_name(subnet.network) == vpc_id`` and
    ``short_name(firewall.network) == vpc_id`` (exact tail match, never
    substring/startswith — a superset name must NOT validate). Emits the
    same ``{valid, errors, validated_subnets, validated_sg}`` shape as the
    AWS provider.
    """
    validation: dict[str, Any] = {
        "valid": True,
        "errors": [],
        "validated_subnets": [],
        "validated_sg": None,
    }

    for subnet_id in subnet_ids:
        try:
            subnet = get_subnetwork(project, region, subnet_id)
        except gax.NotFound:
            validation["valid"] = False
            validation["errors"].append(f"Subnet {subnet_id} not found in region {region}")
            continue
        subnet_network = short_name(subnet.network)
        if subnet_network != vpc_id:
            validation["valid"] = False
            validation["errors"].append(f"Subnet {subnet_id} belongs to network {subnet_network}, not {vpc_id}")
        else:
            validation["validated_subnets"].append(subnet_id)

    if sg_id:
        try:
            firewall = get_firewall(project, sg_id)
        except gax.NotFound:
            validation["valid"] = False
            validation["errors"].append(f"Firewall {sg_id} not found")
        else:
            fw_network = short_name(firewall.network)
            if fw_network != vpc_id:
                validation["valid"] = False
                validation["errors"].append(f"Firewall {sg_id} belongs to network {fw_network}, not {vpc_id}")
            else:
                validation["validated_sg"] = sg_id

    return validation


def _parse_ping_latency(stdout: str) -> float | None:
    """Parse the average RTT (ms) from ``ping`` summary output.

    Linux ping emits a ``rtt min/avg/max/mdev = a/b/c/d ms`` line; the avg
    is the second field. Returns ``None`` when the line is absent (no
    replies) so the caller does not fabricate a latency.
    """
    for line in stdout.splitlines():
        if "min/avg/max" in line and "=" in line:
            parts = line.split("=", 1)[1].strip().split("/")
            if len(parts) >= 2:
                try:
                    return float(parts[1])
                except ValueError:
                    return None
    return None


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test VPC connectivity on Compute Engine")
    parser.add_argument("--vpc-id", required=True, help="Shared network short name")
    parser.add_argument("--subnet-ids", required=True, help="Comma-separated subnetwork short names")
    parser.add_argument("--sg-id", required=True, help="Shared firewall rule name")
    parser.add_argument("--region", required=True, help="GCP region of the subnetworks")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region narrowing)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)
    subnet_ids = [s.strip() for s in args.subnet_ids.split(",") if s.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "vpc_id": args.vpc_id,
        # No SSM-equivalent role is required on Compute Engine.
        "iam_profile": None,
        "tests": {},
        "instances": [],
    }

    # Per-resource cleanup trackers — stamped BEFORE the async wait so a
    # wait-side failure still tears the resource down in finally.
    instance_names: list[str] = []
    intra_fw_name: str | None = None
    key_priv: str | None = None
    key_created = False

    try:
        # 1. Validate inputs FIRST — subnets + firewall must belong to the
        # supplied network (scope-binding exact tail match). Bail before
        # launching anything if validation fails.
        vpc_validation = validate_vpc_resources(project, args.region, args.vpc_id, subnet_ids, args.sg_id)
        result["vpc_validation"] = vpc_validation
        if not vpc_validation["valid"]:
            result["error"] = f"VPC validation failed: {'; '.join(vpc_validation['errors'])}"
            print(json.dumps(result, indent=2, default=str))
            return 1

        if not subnet_ids:
            result["error"] = "No subnet IDs supplied"
            print(json.dumps(result, indent=2, default=str))
            return 1

        # 2. Local SSH keypair (verified-reuse; created bool gates cleanup).
        key_name = unique_suffix("isv-conn-key")
        key_priv, key_created = generate_ssh_keypair(key_name)
        ssh_pubkey = read_ssh_pubkey(key_priv)

        # 3. Intra-VPC ICMP + TCP firewall. The shared --sg-id rule already
        # allows tcp:22 + icmp from 0.0.0.0/0, but it carries no target
        # tags. To keep the VM1->VM2 PRIVATE probe self-sufficient (and not
        # depend on the shared rule's exact shape) we add a tag-scoped
        # INGRESS rule sourced from the validated subnets' CIDR ranges. An
        # allow rule MUST carry at least one Allowed with I_p_protocol set
        # (empty allowed[] -> HTTP 400).
        subnet_ranges: list[str] = []
        for subnet_id in subnet_ids:
            subnet = get_subnetwork(project, args.region, subnet_id)
            if subnet.ip_cidr_range:
                subnet_ranges.append(subnet.ip_cidr_range)
        intra_fw_name = unique_suffix("isv-conn-intra-fw")
        intra_fw = build_firewall(
            intra_fw_name,
            args.vpc_id,
            project,
            direction="INGRESS",
            allowed=[make_allowed("icmp"), make_allowed("tcp", ["22"])],
            source_ranges=subnet_ranges or ["10.0.0.0/8"],
            target_tags=[ISV_NETWORK_TAG],
        )
        insert_firewall(project, intra_fw)

        # 4. Launch TWO probe VMs. VM2 goes in subnet_ids[1] if present,
        # else subnet_ids[0]. Stamp each name as a cleanup tracker
        # IMMEDIATELY after the insert ack, BEFORE the wait.
        launch_specs = [
            (unique_suffix("isv-conn-vm0"), subnet_ids[0]),
            (unique_suffix("isv-conn-vm1"), subnet_ids[1] if len(subnet_ids) > 1 else subnet_ids[0]),
        ]
        for name, subnet_id in launch_specs:
            instance = build_probe_instance(
                project=project,
                zone=zone,
                name=name,
                network_name=args.vpc_id,
                subnet_name=subnet_id,
                ssh_user=DEFAULT_SSH_USER,
                ssh_pubkey=ssh_pubkey,
                external_ip=True,
                network_tags=[ISV_NETWORK_TAG],
            )
            instance_names.append(name)  # cleanup tracker BEFORE the wait
            insert_instance(project, zone, instance)

        # 5. Gate on observable completion: poll BOTH probe VMs to canonical
        # 'running' before reading IPs or probing. The zonal insert op can ack
        # before a VM is observable as running and networking-ready, so reading
        # IPs / SSHing first can flake instance_to_instance even when VPC and
        # firewall connectivity are correct (oracle parity: AWS waits on the
        # instance_running waiter; sister stubs poll each VM to 'running').
        for name, _subnet_id in launch_specs:
            poll_instance_state(project, zone, name, target_canonical="running", timeout=300)

        # Read back IPs + bind the contract's instances[] entries.
        instances_info: list[dict[str, Any]] = []
        for name, subnet_id in launch_specs:
            inst = get_instance(project, zone, name)
            public_ip = first_external_ip(inst) or wait_for_public_ip(project, zone, name, timeout=120)
            instances_info.append(
                {
                    "instance_id": name,
                    "subnet_id": subnet_id,
                    "private_ip": first_internal_ip(inst),
                    "public_ip": public_ip,
                    "vpc_id": short_name(inst.network_interfaces[0].network)
                    if inst.network_interfaces
                    else args.vpc_id,
                }
            )
        result["instances"] = instances_info

        vm1 = instances_info[0]
        vm2 = instances_info[1]
        if not vm1["public_ip"]:
            raise RuntimeError(f"Probe VM {vm1['instance_id']} has no external IP for SSH")

        # 6. Gate on SSH stability (not first-SSH) before probing.
        if not wait_for_ssh(vm1["public_ip"], DEFAULT_SSH_USER, key_priv):
            raise RuntimeError(f"SSH not reachable on {vm1['public_ip']}")
        if not wait_for_ssh_stable(vm1["public_ip"], DEFAULT_SSH_USER, key_priv):
            raise RuntimeError(f"SSH did not stabilize on {vm1['public_ip']}")

        # 7. instance_to_instance: SSH into VM1, ping VM2's PRIVATE IP.
        i2i: dict[str, Any] = {"passed": False, "latency_ms": None}
        if vm2["private_ip"]:
            rc, out, _err = ssh_run(
                vm1["public_ip"],
                DEFAULT_SSH_USER,
                key_priv,
                f"ping -c 3 -W 2 {vm2['private_ip']}",
                timeout=30,
            )
            i2i["passed"] = rc == 0
            i2i["latency_ms"] = _parse_ping_latency(out)
        else:
            i2i["error"] = f"Probe VM {vm2['instance_id']} has no private IP"
        result["tests"]["instance_to_instance"] = i2i

        # 8. instance_to_internet: SSH into VM1, ping 8.8.8.8.
        rc, _out, _err = ssh_run(
            vm1["public_ip"],
            DEFAULT_SSH_USER,
            key_priv,
            "ping -c 3 -W 2 8.8.8.8",
            timeout=30,
        )
        result["tests"]["instance_to_internet"] = {"passed": rc == 0}

        result["success"] = vpc_validation["valid"] and all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Cleanup the resources THIS step created regardless of outcome. The
        # shared VPC / shared firewall are NOT touched — only this step's
        # probe VMs, intra-VPC firewall, and local key. delete_with_retry
        # never raises and returns False only on exhausted retries — capture
        # every cloud-delete bool so a leaked resource fails the step instead
        # of coexisting with success=True. Each delete is gated independently,
        # so a failed sibling never skips the rest.
        cleanup_errors: list[str] = []
        for name in instance_names:
            if not delete_with_retry(
                delete_instance,
                project,
                zone,
                name,
                resource_desc=f"instance {name}",
                timeout=_CLEANUP_INSTANCE_WAIT_S,
            ):
                cleanup_errors.append(f"instance {name}")
        if intra_fw_name and not delete_with_retry(
            delete_firewall,
            project,
            intra_fw_name,
            resource_desc=f"firewall {intra_fw_name}",
            timeout=_CLEANUP_FIREWALL_WAIT_S,
        ):
            cleanup_errors.append(f"firewall {intra_fw_name}")
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
