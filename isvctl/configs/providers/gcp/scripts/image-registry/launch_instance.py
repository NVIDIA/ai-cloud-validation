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

"""Launch a Compute Engine instance from the imported machine image.

Mirrors the AWS reference's ``launch_instance.py`` (create key pair + SSH security
group + instance from the imported image, wait running, read public IP),
translated to Compute Engine:

  * GCP has **no managed key-pair store** — the SSH public key is injected via
    instance metadata and the artifact that survives the run is the local PEM
    pair. ``key_path`` / ``key_name`` carry that pair (``key_created`` gates its
    teardown).
  * GCP has **no security-group resource** — host SSH ingress is a project-scoped
    **VPC firewall rule**, emitted as ``security_group_id`` (its name) so
    teardown can delete exactly the rule this step created (``firewall_created``).
    The rule's ``sourceRanges`` come ONLY from the operator-trusted env var
    ``NETWORK_FIREWALL_TRUST_IP`` — there is no open-internet fallback.
  * GCP has **no instance-profile resource** — the analog is an attached service
    account. This step does not need one (the suite validators are SSH-based, not
    a managed command channel), so ``instance_profile`` is emitted empty and
    teardown skips it when none was created.
  * GPU machine types are subject to L4 zone stockout, so the insert walks the
    reviewed preferred-zone list and reclaims partial async-insert records
    (``leaked_zones``) before advancing — the four documented stockout wire
    shapes are classified uniformly via ``is_zone_unavailable``.

Required JSON output (suite ``vm_from_image`` + ``vm_ssh`` groups):
    {
        "success":           bool,
        "platform":          "image_registry",
        "instance_id":       str,   # Instance.name
        "public_ip":         str,   # networkInterfaces[].accessConfigs[].natIP
        "key_path":          str,   # local SSH private-key path (FieldExistsCheck)
        "key_file":          str,   # same path under the cross-domain SSH-prereq name
        "ssh_user":          str,   # "ubuntu" — ConnectivityCheck / OsCheck login user
        "state":             "running",   # mapped from Instance.status RUNNING (InstanceStateCheck)
        "key_name":          str,   # SSH key resource name (forwarded to teardown --key-name)
        "security_group_id": str,   # VPC firewall rule name (forwarded to teardown --security-group-id)
        "instance_profile":  str,   # attached service-account email, or "" when none created
        ...                          # zone / firewall_created / key_created / instance_created / leaked_zones
    }

Usage:
    python launch_instance.py --image-id <name> --instance-type g2-standard-8 --region <region>

AWS reference implementation:
    ../../aws/scripts/image-registry/launch_instance.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    ISV_NETWORK_TAG,
    delete_failed_zonal_instance,
    delete_local_keypair,
    first_external_ip,
    first_internal_ip,
    generate_ssh_keypair,
    get_instance,
    insert_ssh_firewall,
    is_gpu_machine_type,
    is_zone_unavailable,
    poll_instance_state,
    read_ssh_pubkey,
    resolve_project,
    resolve_trusted_ssh_source_ranges,
    select_zones,
    short_name,
    unique_suffix,
    wait_for_global_op,
    wait_for_public_ip,
    wait_for_zonal_op,
)
from common.errors import classify_gcp_error, delete_with_retry, handle_gcp_errors
from common.ssh_utils import wait_for_cloud_init, wait_for_ssh, wait_for_ssh_stable
from google.api_core import exceptions as gax
from google.cloud import compute_v1

DEFAULT_NETWORK = "default"
DEFAULT_SSH_USER = "ubuntu"

# Per-attempt cleanup-on-failure op waits — bounded so a 3-attempt
# delete_with_retry does not multiply into the enclosing step budget.
_CLEANUP_INSTANCE_WAIT_S = 180
_CLEANUP_FIREWALL_WAIT_S = 120
# Happy-path op waits.
_INSERT_OP_WAIT_S = 600
_FIREWALL_OP_WAIT_S = 120
_RUNNING_POLL_S = 300
_PUBLIC_IP_POLL_S = 120


def _build_instance_resource(
    *,
    project: str,
    zone: str,
    name: str,
    machine_type: str,
    source_image: str,
    network_name: str,
    ssh_user: str,
    ssh_pubkey: str,
) -> compute_v1.Instance:
    """Build a Compute Engine ``Instance`` to launch from the imported image."""
    instance = compute_v1.Instance()
    instance.name = name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"
    instance.description = "ISV image-registry launch-from-image instance (createdby=isvtest)"

    boot = compute_v1.AttachedDisk()
    boot.boot = True
    boot.auto_delete = True
    init = compute_v1.AttachedDiskInitializeParams()
    init.source_image = source_image
    init.disk_size_gb = 50
    boot.initialize_params = init
    instance.disks = [boot]

    nic = compute_v1.NetworkInterface()
    nic.network = f"projects/{project}/global/networks/{network_name}"
    nat = compute_v1.AccessConfig()
    nat.type_ = "ONE_TO_ONE_NAT"
    nat.name = "External NAT"
    nic.access_configs = [nat]
    instance.network_interfaces = [nic]

    # Network tag must match the SSH firewall's target tag so the rule selects
    # this instance.
    instance.tags = compute_v1.Tags(items=[ISV_NETWORK_TAG])

    # GPU machine types REJECT the default MIGRATE maintenance policy and
    # require TERMINATE + automatic_restart; non-GPU types keep the API default.
    if is_gpu_machine_type(machine_type):
        sched = compute_v1.Scheduling()
        sched.on_host_maintenance = "TERMINATE"
        sched.automatic_restart = True
        instance.scheduling = sched

    ssh_item = compute_v1.Items()
    ssh_item.key = "ssh-keys"
    ssh_item.value = f"{ssh_user}:{ssh_pubkey}"
    instance.metadata = compute_v1.Metadata(items=[ssh_item])

    return instance


def _delete_instance_op(project: str, zone: str, name: str) -> None:
    """Submit instances.delete and wait on the zonal op (NotFound idempotent)."""
    try:
        op = compute_v1.InstancesClient().delete(project=project, zone=zone, instance=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=_CLEANUP_INSTANCE_WAIT_S)


def _delete_firewall_op(project: str, name: str) -> None:
    """Submit firewalls.delete and wait on the global op (NotFound idempotent)."""
    try:
        op = compute_v1.FirewallsClient().delete(project=project, firewall=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_CLEANUP_FIREWALL_WAIT_S)


@handle_gcp_errors
def main() -> int:
    """Launch an instance from the imported image and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Launch a Compute Engine instance from an imported image")
    parser.add_argument("--image-id", required=True, help="Imported machine image name (from upload_image)")
    parser.add_argument("--instance-type", default="g2-standard-8", help="Compute Engine machine type")
    parser.add_argument("--region", required=True, help="GCP region (zone-walk scope)")
    parser.add_argument("--zone", default=None, help="GCP zone pin (disables the zone walk)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--network", default=DEFAULT_NETWORK, help="VPC network name for the NIC + firewall")
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER, help="SSH login user (ConnectivityCheck / OsCheck)")
    parser.add_argument("--key-name", default="isv-ir-key", help="Base name for the local SSH key pair")
    parser.add_argument("--firewall-name", default="isv-ir-ssh", help="Base name for the SSH firewall rule")
    args = parser.parse_args()

    project = resolve_project(args.project)
    instance_name = unique_suffix("isv-ir-instance")
    key_name = unique_suffix(args.key_name)
    fw_name = unique_suffix(args.firewall_name)
    network_name = args.network if args.network and args.network.lower() != "none" else DEFAULT_NETWORK

    # Multi-zone walk candidates (single-zone pin honored when --zone is set).
    candidate_zones = select_zones(args.zone or args.region, project=project)

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "instance_id": "",
        "public_ip": "",
        "key_path": "",
        "key_file": "",
        "ssh_user": args.ssh_user,
        "state": "",
        "key_name": key_name,
        "security_group_id": "",
        "firewall_name": "",
        "instance_profile": "",  # no attached SA created — empty per the GCP gap
        "image_id": args.image_id,
        "instance_created": False,
        "firewall_created": False,
        "key_created": False,
        "zone": "",
        "leaked_zones": [],
    }

    # Tracker state for cleanup-on-failure.
    instance_created = False
    firewall_created = False
    key_created = False
    key_priv = ""
    zone = ""

    try:
        # 1. Trusted SSH ingress — fail closed when NETWORK_FIREWALL_TRUST_IP is
        # unset/invalid/open-internet (there is no fallback range).
        ssh_source_ranges = resolve_trusted_ssh_source_ranges()

        # 2. Local SSH key pair (verified-reuse; created flag gates teardown).
        key_priv, key_created = generate_ssh_keypair(key_name)
        result["key_path"] = key_priv
        result["key_file"] = key_priv
        result["key_created"] = key_created
        ssh_pubkey = read_ssh_pubkey(key_priv)

        # 3. SSH firewall rule (verified-reuse). insert returns op=None on
        # adoption of an already-matching rule.
        fw_name, fw_op = insert_ssh_firewall(
            project=project,
            name=fw_name,
            network_short=network_name,
            source_ranges=ssh_source_ranges,
        )
        result["firewall_name"] = fw_name
        result["security_group_id"] = fw_name
        if fw_op is not None:
            firewall_created = True
            result["firewall_created"] = True
            wait_for_global_op(project, fw_op.name, timeout=_FIREWALL_OP_WAIT_S)

        # 4. Build + insert with multi-zone stockout walk.
        source_image = f"projects/{project}/global/images/{args.image_id}"
        instances_client = compute_v1.InstancesClient()
        last_error: Exception | None = None
        for idx, candidate_zone in enumerate(candidate_zones, start=1):
            print(
                f"Inserting instance {instance_name} in {project}/{candidate_zone} [{idx}/{len(candidate_zones)}]...",
                file=sys.stderr,
            )
            instance_resource = _build_instance_resource(
                project=project,
                zone=candidate_zone,
                name=instance_name,
                machine_type=args.instance_type,
                source_image=source_image,
                network_name=network_name,
                ssh_user=args.ssh_user,
                ssh_pubkey=ssh_pubkey,
            )
            op = None
            try:
                op = instances_client.insert(
                    project=project,
                    zone=candidate_zone,
                    instance_resource=instance_resource,
                )
                # Stamp-before-wait: record ownership + identifiers on the
                # insert ack so a wait-side failure still hands teardown a
                # truthful target.
                instance_created = True
                zone = candidate_zone
                result["instance_id"] = instance_name
                result["instance_created"] = True
                result["zone"] = candidate_zone
                op_name = getattr(op, "name", "")
                if op_name:
                    wait_for_zonal_op(project, candidate_zone, op_name, timeout=_INSERT_OP_WAIT_S)
                break
            except Exception as exc:
                if not is_zone_unavailable(exc, op=op):
                    raise
                last_error = exc
                # Shape 2/4: reclaim a partial async-insert before walking on.
                if instance_created:
                    cleaned = delete_failed_zonal_instance(project, candidate_zone, instance_name)
                    if not cleaned:
                        result["leaked_zones"].append(candidate_zone)
                    instance_created = False
                else:
                    result["leaked_zones"].append(candidate_zone)
                print(f"  walking past {candidate_zone} (stockout-class)", file=sys.stderr)
                continue
        else:
            raise RuntimeError(f"Zone-walk exhausted ({len(candidate_zones)} candidates); last error: {last_error}")

        # 5. Poll canonical RUNNING.
        print("Waiting for RUNNING status...", file=sys.stderr)
        result["state"] = poll_instance_state(
            project,
            zone,
            instance_name,
            target_canonical="running",
            timeout=_RUNNING_POLL_S,
        )

        # 6. Re-read for IPs (ephemeral external IP is only populated once running).
        inst = get_instance(project, zone, instance_name)
        result["public_ip"] = first_external_ip(inst) or wait_for_public_ip(
            project, zone, instance_name, timeout=_PUBLIC_IP_POLL_S
        )
        if not result["public_ip"]:
            raise RuntimeError("Instance reached RUNNING but no external IP became observable")
        result["private_ip"] = first_internal_ip(inst)
        result["vpc_id"] = short_name(inst.network_interfaces[0].network)

        # 7. SSH-readiness gate. The suite vm_ssh validators (ConnectivityCheck,
        # OsCheck) open a fresh SSH session the instant this step returns, so a
        # bare RUNNING + external-IP signal is NOT enough: the imported Ubuntu
        # image still has to boot, let cloud-init inject the ssh-keys-metadata
        # key for the login user, and survive the guest-agent sshd restart that
        # follows cloud-init. Without this gate the validators race an unready /
        # bouncing sshd and fail with "Unable to connect to port 22". Mirrors
        # the gcp/vm launch readiness gate (wait_for_ssh -> cloud-init -> stable;
        # fatal only when NEITHER SSH nor cloud-init becomes observable).
        ssh_ok = wait_for_ssh(
            host=result["public_ip"],
            user=args.ssh_user,
            key_file=key_priv,
            max_attempts=30,
            interval=10,
        )
        cloud_init_ok = False
        if ssh_ok:
            cloud_init_ok = wait_for_cloud_init(
                host=result["public_ip"],
                user=args.ssh_user,
                key_file=key_priv,
                timeout_seconds=420,
            )
            if cloud_init_ok:
                # Guest-agent restarts sshd shortly after cloud-init completes
                # (refreshes authorized_keys / host keys); require 3 consecutive
                # SSH successes so the bounce is washed out before the validators
                # connect via paramiko.
                ssh_stable_ok = wait_for_ssh_stable(
                    host=result["public_ip"],
                    user=args.ssh_user,
                    key_file=key_priv,
                    consecutive=3,
                    interval=10,
                    max_attempts=18,
                )
                if not ssh_stable_ok:
                    print("  SSH did not stabilize after cloud-init; continuing best-effort", file=sys.stderr)
                result["ssh_stable"] = ssh_stable_ok
        result["ssh_ready"] = ssh_ok
        result["cloud_init_ok"] = cloud_init_ok
        if not (ssh_ok or cloud_init_ok):
            raise RuntimeError("Instance reached RUNNING but SSH did not become reachable within the readiness budget")

        result["success"] = True
        print(f"Launch succeeded: {instance_name} @ {result['public_ip']}", file=sys.stderr)
    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        result.setdefault("error_type", error_type)
        result["error"] = error_msg
        result["success"] = False
        # Cleanup-on-failure — gate each delete on its ownership tracker so a
        # verified-reuse-adopted firewall / pre-existing key is never destroyed.
        try:
            if instance_created and zone:
                delete_with_retry(
                    _delete_instance_op, project, zone, instance_name, resource_desc=f"instance {instance_name}"
                )
            if firewall_created:
                delete_with_retry(_delete_firewall_op, project, fw_name, resource_desc=f"firewall {fw_name}")
            if key_created and key_priv:
                delete_local_keypair(key_priv)
        except Exception as cleanup_exc:
            print(f"Cleanup-on-failure error: {cleanup_exc}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
