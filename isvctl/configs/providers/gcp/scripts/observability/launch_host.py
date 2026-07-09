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

"""Launch the observability syslog-probe host on the run subnetwork (setup phase).

Translates the AWS oracle's bare-metal ``launch_instance`` (managed key pair +
security group + run_instances) onto Compute Engine. Documented divergences:

  * GCP has NO managed key-pair store — the SSH public key is injected via
    instance ``ssh-keys`` metadata and the surviving artifact is the local PEM
    pair (``key_file``, gated on ``key_created`` for teardown).
  * GCP has NO security-group resource — host SSH ingress is a project-scoped VPC
    firewall rule targeted by an instance network tag. Its ``sourceRanges`` come
    ONLY from the operator-trusted env var ``NETWORK_FIREWALL_TRUST_IP`` — there
    is no open-internet fallback (emitted as ``firewall_name``, gated on
    ``firewall_created``).
  * Compute Engine instances are ZONAL and the subnetwork is regional, so the
    launch walks only the OPERATOR REGION's zones (never cross-region — the
    regional subnet would not exist elsewhere) and emits the successful ``zone``
    for teardown. Partial async-insert records left by a stockout-class failure
    are reclaimed before advancing; any that cannot be inline-confirmed are
    recorded in ``leaked_zones`` for teardown to reclaim exactly.

This host is CPU-only (default ``e2-standard-2``); the syslog probe needs neither
Docker nor a GPU. A guest-readiness gate (SSH -> cloud-init -> stable SSH) runs so
the downstream ``host_syslogs`` SSH probe connects to a settled sshd.

Emits:
    {
        "success":          bool,
        "platform":         "observability",
        "instance_id":      str,        # Instance.name
        "zone":             str,        # effective successful zone
        "leaked_zones":     [str, ...], # partial-insert phantoms for teardown
        "public_ip":        str,        # accessConfigs[].natIP
        "key_file":         str,        # local SSH private-key path
        "ssh_user":         str,
        "instance_created": bool,
        "firewall_name":    str,
        "firewall_created": bool,
        "key_created":      bool,
        ...
    }

AWS reference implementation:
    ../../aws/scripts/bare_metal/launch_instance.py (launch_host reuses the bm stub)
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    ISV_NETWORK_TAG,
    delete_failed_zonal_instance,
    delete_local_keypair,
    first_external_ip,
    generate_ssh_keypair,
    get_instance,
    insert_ssh_firewall,
    is_zone_unavailable,
    poll_instance_state,
    read_ssh_pubkey,
    resolve_image,
    resolve_project,
    resolve_trusted_ssh_source_ranges,
    unique_suffix,
    wait_for_global_op,
    wait_for_public_ip,
    wait_for_zonal_op,
)
from common.errors import classify_gcp_error, delete_with_retry, handle_gcp_errors
from common.network import region_zones, subnetwork_url
from common.ssh_utils import wait_for_cloud_init, wait_for_ssh, wait_for_ssh_stable
from google.cloud import compute_v1

DEFAULT_SSH_USER = "ubuntu"
DEFAULT_IMAGE = "ubuntu-2204-lts"
DEFAULT_IMAGE_PROJECT = "ubuntu-os-cloud"

# Per-attempt cleanup-on-failure op waits — bounded so a 3-attempt
# delete_with_retry does not multiply into the enclosing step budget.
_CLEANUP_INSTANCE_WAIT_S = 180
_CLEANUP_FIREWALL_WAIT_S = 120
# Happy-path op / readiness waits (all deadline-bounded; the step timeout is
# headroom over their worst-case sum, not their product).
_INSERT_OP_WAIT_S = 300
_FIREWALL_OP_WAIT_S = 120
_RUNNING_POLL_S = 300
_PUBLIC_IP_POLL_S = 120
_SSH_ATTEMPTS = 20
_SSH_INTERVAL = 10
_CLOUD_INIT_WAIT_S = 300


def _build_instance_resource(
    *,
    project: str,
    zone: str,
    region: str,
    name: str,
    machine_type: str,
    source_image: str,
    network_name: str,
    subnet_name: str,
    ssh_user: str,
    ssh_pubkey: str,
) -> compute_v1.Instance:
    """Build the observability host Instance bound to the run subnetwork."""
    instance = compute_v1.Instance()
    instance.name = name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"
    instance.description = "ISV observability syslog-probe host (createdby=isvtest)"

    boot = compute_v1.AttachedDisk()
    boot.boot = True
    boot.auto_delete = True
    init = compute_v1.AttachedDiskInitializeParams()
    init.source_image = source_image
    init.disk_size_gb = 20
    boot.initialize_params = init
    instance.disks = [boot]

    nic = compute_v1.NetworkInterface()
    nic.network = f"projects/{project}/global/networks/{network_name}"
    nic.subnetwork = subnetwork_url(project, region, subnet_name)
    nat = compute_v1.AccessConfig()
    nat.type_ = "ONE_TO_ONE_NAT"
    nat.name = "External NAT"
    nic.access_configs = [nat]
    instance.network_interfaces = [nic]

    # Network tag must match the SSH firewall's target tag so the rule selects
    # this instance.
    instance.tags = compute_v1.Tags(items=[ISV_NETWORK_TAG])

    ssh_item = compute_v1.Items()
    ssh_item.key = "ssh-keys"
    ssh_item.value = f"{ssh_user}:{ssh_pubkey}"
    instance.metadata = compute_v1.Metadata(items=[ssh_item])

    return instance


def _delete_instance_op(project: str, zone: str, name: str) -> None:
    """Submit instances.delete and wait on the zonal op (NotFound idempotent)."""
    from google.api_core import exceptions as gax

    try:
        op = compute_v1.InstancesClient().delete(project=project, zone=zone, instance=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=_CLEANUP_INSTANCE_WAIT_S)


def _delete_firewall_op(project: str, name: str) -> None:
    """Submit firewalls.delete and wait on the global op (NotFound idempotent)."""
    from google.api_core import exceptions as gax

    try:
        op = compute_v1.FirewallsClient().delete(project=project, firewall=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_CLEANUP_FIREWALL_WAIT_S)


@handle_gcp_errors
def main() -> int:
    """Launch the observability host and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Launch the GCP observability syslog-probe host")
    parser.add_argument("--name", default="isv-observability-host", help="Instance name prefix (run-id suffixed)")
    parser.add_argument("--instance-type", default="e2-standard-2", help="Compute Engine machine type (CPU-only)")
    parser.add_argument("--region", required=True, help="GCP region (subnet region + in-region zone walk scope)")
    parser.add_argument("--zone", default=None, help="GCP zone pin (single-zone; disables the in-region walk)")
    parser.add_argument("--vpc-id", required=True, help="Compute Engine network name for the NIC")
    parser.add_argument("--subnet-id", required=True, help="Regional subnetwork name for the NIC")
    parser.add_argument("--ami-id", default=DEFAULT_IMAGE, help="Image short-name or family (resolved under project)")
    parser.add_argument("--image-project", default=DEFAULT_IMAGE_PROJECT, help="Project owning the image")
    parser.add_argument("--firewall-name", default="isv-observability-ssh", help="SSH firewall name prefix")
    parser.add_argument("--key-name", default="isv-observability-host-key", help="Local SSH key pair name prefix")
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER, help="Guest SSH login user")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    # A run-id-only suffix is NOT enough: parallel step-isolation jobs share a
    # single RUN_ID, so run-id-only instance / firewall / key names are identical
    # across those sibling jobs and collide on AlreadyExists. Fold a
    # per-invocation discriminator (4 hex chars) BETWEEN each base and the run-id
    # suffix so every invocation gets fresh names; the run id stays TERMINAL so
    # the run-id-scoped orphan sweep (which matches names ending in the run id)
    # still recognizes them. The full names are emitted and forwarded verbatim to
    # teardown_host / teardown_network, which never reconstruct them.
    disc = secrets.token_hex(2)  # 4 hex chars, fresh per invocation
    instance_name = unique_suffix(f"{args.name}-{disc}")
    key_name = unique_suffix(f"{args.key_name}-{disc}")
    fw_name = unique_suffix(f"{args.firewall_name}-{disc}")
    image_arg = args.ami_id if args.ami_id and args.ami_id.lower() != "none" else DEFAULT_IMAGE
    image_project = (
        args.image_project if args.image_project and args.image_project.lower() != "none" else DEFAULT_IMAGE_PROJECT
    )

    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "launch_host",
        "instance_id": "",
        "zone": "",
        "leaked_zones": [],
        "public_ip": "",
        "key_file": "",
        "ssh_user": args.ssh_user,
        "firewall_name": "",
        "firewall_created": False,
        "key_created": False,
        "instance_created": False,
    }

    # Tracker state for cleanup-on-failure.
    instance_created = False
    firewall_created = False
    key_created = False
    key_priv = ""
    zone = ""

    try:
        # 1. Trusted SSH ingress — fail closed when NETWORK_FIREWALL_TRUST_IP is
        # unset / invalid / open-internet (there is no fallback range).
        ssh_source_ranges = resolve_trusted_ssh_source_ranges()

        # 2. Resolve the boot image (operator project first, then family alias).
        image = resolve_image(image_project, image_arg)
        source_image = image.self_link

        # 3. Local SSH key pair (verified-reuse; created flag gates teardown).
        key_priv, key_created = generate_ssh_keypair(key_name)
        result["key_file"] = key_priv
        result["key_created"] = key_created
        ssh_pubkey = read_ssh_pubkey(key_priv)

        # 4. SSH firewall rule on the run network (verified-reuse). insert returns
        # op=None on adoption of an already-matching rule.
        fw_name, fw_op = insert_ssh_firewall(
            project=project,
            name=fw_name,
            network_short=args.vpc_id,
            source_ranges=ssh_source_ranges,
        )
        result["firewall_name"] = fw_name
        if fw_op is not None:
            firewall_created = True
            result["firewall_created"] = True
            wait_for_global_op(project, fw_op.name, timeout=_FIREWALL_OP_WAIT_S)

        # 5. Insert with an in-region zone walk. The subnetwork is regional, so
        # candidates are the operator region's zones ONLY — never cross-region.
        if args.zone and args.zone.lower() != "none":
            candidate_zones = [args.zone]
        else:
            candidate_zones = region_zones(project, args.region)
        if not candidate_zones:
            raise RuntimeError(f"region {args.region!r} reports no zones for the host launch")

        instances_client = compute_v1.InstancesClient()
        last_error: Exception | None = None
        for idx, candidate_zone in enumerate(candidate_zones, start=1):
            print(
                f"Inserting host {instance_name} in {project}/{candidate_zone} [{idx}/{len(candidate_zones)}]...",
                file=sys.stderr,
            )
            instance_resource = _build_instance_resource(
                project=project,
                zone=candidate_zone,
                region=args.region,
                name=instance_name,
                machine_type=args.instance_type,
                source_image=source_image,
                network_name=args.vpc_id,
                subnet_name=args.subnet_id,
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
                # Stamp-before-wait: record ownership + identifiers on the insert
                # ack so a wait-side failure still hands teardown a truthful target.
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
                # Stockout-class shape. Only an ACCEPTED insert can leave a
                # phantom to reclaim: instance_created is stamped only after the
                # synchronous insert returns, so if insert() itself raised
                # (nothing accepted in this zone) there is no record to delete and
                # NOTHING is leaked — recording a leaked zone there would send
                # teardown chasing an instance that was never created. Add the
                # zone to leaked_zones ONLY when an accepted insert's reclaim
                # delete could not be confirmed.
                if instance_created:
                    cleaned = delete_failed_zonal_instance(project, candidate_zone, instance_name)
                    if not cleaned:
                        result["leaked_zones"].append(candidate_zone)
                    instance_created = False
                    result["instance_created"] = False
                print(f"  walking past {candidate_zone} (stockout-class)", file=sys.stderr)
                continue
        else:
            raise RuntimeError(f"Zone-walk exhausted ({len(candidate_zones)} candidates); last error: {last_error}")

        # 6. Poll canonical RUNNING.
        print("Waiting for RUNNING status...", file=sys.stderr)
        poll_instance_state(project, zone, instance_name, target_canonical="running", timeout=_RUNNING_POLL_S)

        # 7. Re-read for the external IP (ephemeral IP is only populated once running).
        inst = get_instance(project, zone, instance_name)
        result["public_ip"] = first_external_ip(inst) or wait_for_public_ip(
            project, zone, instance_name, timeout=_PUBLIC_IP_POLL_S
        )
        if not result["public_ip"]:
            raise RuntimeError("Host reached RUNNING but no external IP became observable")

        # 8. Guest-readiness gate so the host_syslogs SSH probe is handed a
        # settled sshd. Launch success requires a running-state read-back PLUS
        # stable SSH readiness, so ALL THREE stages must pass — initial SSH,
        # cloud-init completion, and the consecutive stable-SSH gate across the
        # guest-agent sshd bounce. Any stage failing is fatal (each raises so
        # cleanup-on-failure runs and the step exits non-zero); an unsettled
        # guest is never reported as a successful launch.
        ssh_ok = wait_for_ssh(
            host=result["public_ip"],
            user=args.ssh_user,
            key_file=key_priv,
            max_attempts=_SSH_ATTEMPTS,
            interval=_SSH_INTERVAL,
        )
        result["ssh_ready"] = ssh_ok
        if not ssh_ok:
            raise RuntimeError(
                "Host reached RUNNING but initial SSH did not become reachable within the readiness budget"
            )

        cloud_init_ok = wait_for_cloud_init(
            host=result["public_ip"],
            user=args.ssh_user,
            key_file=key_priv,
            timeout_seconds=_CLOUD_INIT_WAIT_S,
        )
        result["cloud_init_ok"] = cloud_init_ok
        if not cloud_init_ok:
            raise RuntimeError(
                "Host reached RUNNING and SSH connected but cloud-init did not complete within the readiness budget"
            )

        ssh_stable_ok = wait_for_ssh_stable(
            host=result["public_ip"],
            user=args.ssh_user,
            key_file=key_priv,
            consecutive=3,
            interval=_SSH_INTERVAL,
            max_attempts=18,
        )
        result["ssh_stable"] = ssh_stable_ok
        if not ssh_stable_ok:
            raise RuntimeError(
                "Host cloud-init completed but SSH did not stay stable across the guest-agent sshd bounce"
            )

        result["success"] = True
        print(f"Launch succeeded: {instance_name} @ {result['public_ip']} ({zone})", file=sys.stderr)
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
            # Reclaim leaked-zone phantoms INDEPENDENT of instance_created: the
            # exhausted-zone-walk stockout path resets instance_created=false yet
            # leaves a populated leaked_zones with the retained deterministic name.
            # Gating this on instance_created (like the landed-zone delete above)
            # would orphan the billable phantom; the run-scoped teardown steps are
            # the load-bearing net but this local pass shrinks the leak window.
            for leak_zone in result["leaked_zones"]:
                if instance_created and leak_zone == zone:
                    continue
                delete_with_retry(
                    _delete_instance_op,
                    project,
                    leak_zone,
                    instance_name,
                    resource_desc=f"instance {instance_name}@{leak_zone}",
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
