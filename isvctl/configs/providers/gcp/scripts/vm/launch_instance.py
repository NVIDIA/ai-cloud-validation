#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Launch a GPU VM on Compute Engine for VM-domain validation.

Translates the AWS oracle's ``launch_instance`` workflow to Compute
Engine. Documented divergences:

  * No managed key-pair store — generate a local PEM/.pub pair and
    attach the public key via instance metadata.
  * Firewall rules are project-global and bound by network tag, not
    attached per-instance — create / verified-reuse a TCP/22 INGRESS
    rule on the launch network and assign the matching network tag.
  * GPU-bearing machine types reject ``onHostMaintenance=MIGRATE``
    (HTTP 400); force ``TERMINATE`` + ``automatic_restart=true``.
  * ``instances.insert`` returns DONE before the guest is reachable —
    poll RUNNING, then run a best-effort SSH-or-cloud-init readiness gate.
  * Public IP is assigned only when an ``accessConfigs`` entry of type
    ``ONE_TO_ONE_NAT`` is requested on the NIC.
  * Compute Engine label keys must be lowercase. Project canonical
    mixed-case ``Name`` / ``CreatedBy`` keys to api-valid labels on
    create and back on read so ``InstanceTagCheck.required_keys`` does
    not change per provider.
  * Emit the effective ``zone``, ``firewall_created``, ``key_created``
    so every downstream zonal step + teardown can read them via
    ``{{steps.launch_instance.X}}`` (verified-reuse cleanup contract).

Operator-supplied image identifiers are resolved against
``args.image_project`` FIRST — short-name identifiers MUST be resolved
against the operator's chosen project/account/region scope first.
A vendor-default fallback is allowed only as an explicit second attempt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    ISV_NETWORK_TAG,
    canonical_state,
    canonical_tags_to_labels,
    delete_failed_zonal_instance,
    delete_local_keypair,
    first_external_ip,
    first_internal_ip,
    generate_ssh_keypair,
    get_instance,
    insert_ssh_firewall,
    is_gpu_machine_type,
    is_zone_unavailable,
    narrow_region_to_zone,
    poll_instance_state,
    read_ssh_pubkey,
    resolve_image,
    resolve_project,
    retry_zonal_lifecycle_op,
    select_zones,
    short_name,
    unique_suffix,
    wait_for_global_op,
    wait_for_public_ip,
    wait_for_zonal_op,
)
from common.errors import delete_with_retry, handle_gcp_errors
from common.ssh_utils import wait_for_cloud_init, wait_for_ssh, wait_for_ssh_stable
from google.api_core import exceptions as gax
from google.cloud import compute_v1

# GCP Deep Learning VM Image — the closest public equivalent to AWS's
# Deep Learning AMIs. Ships with the NVIDIA driver + CUDA toolkit
# preinstalled. Does NOT ship Docker; for tests that require a container
# runtime (e.g. the NIM deploy step) operators must either supply a
# custom image via --image-project / --image-family / --ami-id, or
# install Docker out-of-band before invoking the suite. The image lives
# in a public GCP-published project so no operator-specific entitlement
# is needed.
DEFAULT_IMAGE_FAMILY = "common-cu129-ubuntu-2204-nvidia-580"
DEFAULT_IMAGE_PROJECT = "deeplearning-platform-release"
DEFAULT_NETWORK = "default"
DEFAULT_FIREWALL_NAME = "isv-test-vm-ssh"
DEFAULT_KEY_NAME = "isv-test-key"
DEFAULT_SSH_USER = "ubuntu"

# Bound the per-attempt wait so the 3-attempt delete_with_retry
# does not multiply 600s zonal-op + 120s global-op budgets into the
# enclosing step timeout. Cleanup-on-failure runs from inside the
# launch_instance step, whose budget already covers happy-path waits;
# delete waits beyond 180s instance / 120s firewall are diminishing
# returns under transient control-plane errors.
_CLEANUP_INSTANCE_WAIT_S = 180
_CLEANUP_FIREWALL_WAIT_S = 120


def _build_instance_resource(
    *,
    project: str,
    zone: str,
    name: str,
    machine_type: str,
    source_image: str,
    network_name: str,
    subnet_name: str | None,
    ssh_user: str,
    ssh_pubkey: str,
    labels: dict[str, str],
) -> compute_v1.Instance:
    """Build a Compute Engine ``Instance`` resource for ``instances.insert``.

    Every property here serializes via proto-plus (so it survives the
    REST encode); ad-hoc ``obj._properties[...] = ...`` mutations would
    be silently dropped.

    Subnetwork (when supplied) MUST be the regional URL; ``machine_type``
    MUST be the zonal URL — bare tokens are rejected by the proto wire
    layer.
    """
    instance = compute_v1.Instance()
    instance.name = name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"

    boot = compute_v1.AttachedDisk()
    boot.boot = True
    boot.auto_delete = True
    init = compute_v1.AttachedDiskInitializeParams()
    init.source_image = source_image
    init.disk_size_gb = 100
    boot.initialize_params = init
    instance.disks = [boot]

    nic = compute_v1.NetworkInterface()
    nic.network = f"projects/{project}/global/networks/{network_name}"
    if subnet_name:
        region = zone.rsplit("-", 1)[0]
        nic.subnetwork = f"projects/{project}/regions/{region}/subnetworks/{subnet_name}"
    nat = compute_v1.AccessConfig()
    nat.type_ = "ONE_TO_ONE_NAT"
    nat.name = "External NAT"
    nic.access_configs = [nat]
    instance.network_interfaces = [nic]

    instance.tags = compute_v1.Tags(items=[ISV_NETWORK_TAG])

    # GPU machine types REJECT the default `MIGRATE` and require
    # `TERMINATE` + `automatic_restart`. This override is GPU-only —
    # non-GPU types must keep the API default (MIGRATE) to preserve
    # live-migrate behavior.
    if is_gpu_machine_type(machine_type):
        sched = compute_v1.Scheduling()
        sched.on_host_maintenance = "TERMINATE"
        sched.automatic_restart = True
        instance.scheduling = sched

    instance.labels = labels

    ssh_item = compute_v1.Items()
    ssh_item.key = "ssh-keys"
    ssh_item.value = f"{ssh_user}:{ssh_pubkey}"
    instance.metadata = compute_v1.Metadata(items=[ssh_item])

    return instance


def _delete_instance_op(project: str, zone: str, name: str) -> None:
    """Submit ``instances.delete`` and wait on the zonal op (NotFound is idempotent)."""
    try:
        op = compute_v1.InstancesClient().delete(project=project, zone=zone, instance=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", None) or getattr(op, "operation", "")
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=_CLEANUP_INSTANCE_WAIT_S)


def _delete_firewall_op(project: str, name: str) -> None:
    """Submit ``firewalls.delete`` and wait on the global op (NotFound is idempotent)."""
    try:
        op = compute_v1.FirewallsClient().delete(project=project, firewall=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", None) or getattr(op, "operation", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_CLEANUP_FIREWALL_WAIT_S)


def _find_ssh_firewall_for_instance(
    project: str,
    inst: compute_v1.Instance,
) -> str | None:
    """Best-effort: derive the SSH firewall name covering ``inst``.

    Used on the reuse-existing-instance path to surface the security
    handle from live state rather than fabricate one. Returns None when
    no rule matches; emitting None is more honest than a stand-in
    (the validator + teardown gating both see "no firewall to manage").
    """
    if not inst.network_interfaces:
        return None
    network_short = short_name(inst.network_interfaces[0].network)
    inst_tags = set(getattr(inst.tags, "items", []) or [])
    if not inst_tags:
        return None
    try:
        rules = compute_v1.FirewallsClient().list(project=project)
    except gax.GoogleAPICallError:
        return None
    for rule in rules:
        if short_name(rule.network) != network_short:
            continue
        if rule.direction != "INGRESS":
            continue
        if not (set(rule.target_tags) & inst_tags):
            continue
        for allowed in rule.allowed:
            if allowed.I_p_protocol.lower() == "tcp" and "22" in list(allowed.ports):
                return rule.name
    return None


def _reuse_existing_instance(
    *,
    project: str,
    zone: str,
    instance_id: str,
    key_file: str,
    ssh_user: str,
) -> int:
    """Mirror the AWS oracle's ``AWS_VM_INSTANCE_ID``/``AWS_VM_KEY_FILE`` reuse path.

    GCP equivalents are ``GCP_VM_INSTANCE_ID`` / ``GCP_VM_KEY_FILE``. When
    both are set, the stub describes the existing instance (and starts it
    if it's canonically stopped) instead of provisioning a new one — the
    dev workflow for iterating against a long-lived VM.

    Verified-reuse semantics: ``firewall_created`` / ``key_created`` stay
    False so teardown's gates skip destruction of pre-existing resources.
    """
    print(f"Reusing existing instance {instance_id}", file=sys.stderr)

    # Reuse-branch must not fabricate keys it doesn't have evidence for.
    # Initialize fields as None and only fill them when live state
    # provides a value.
    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": instance_id,
        "project": project,
        "zone": zone,
        "availability_zone": zone,
        "key_file": key_file,
        "key_created": False,
        "firewall_created": False,
        # Verified-reuse ownership: adoption path NEVER created the
        # instance, so teardown must skip both the primary and any
        # leaked-zone delete. False stays False — there is no in-stub
        # branch that could promote it on the reuse path.
        "instance_created": False,
        "firewall_name": None,
        "security_group_id": None,
        "key_name": None,
        "ssh_user": ssh_user,
        "reused": True,
        "tags": {},
    }

    started_in_reuse = False
    try:
        inst = get_instance(project, zone, instance_id)
        cstate = canonical_state(inst.status)

        if cstate == "stopped":
            print(f"  {instance_id} is stopped — starting it", file=sys.stderr)
            # Sister-stub consistency (rule #4): the dedicated
            # `start_instance.py` wraps the start sync+wait pair in the
            # in-zone retry-with-backoff envelope (3 attempts, 60s/120s
            # backoff). The reuse-from-stopped path runs the SAME
            # lifecycle op against the SAME zone-bound instance and MUST
            # honor the same recovery contract — operators stockout-flake
            # here exactly as they would on the canonical start step.
            client = compute_v1.InstancesClient()
            retry_zonal_lifecycle_op(
                lambda: client.start(project=project, zone=zone, instance=instance_id),
                project,
                zone,
                resource_desc=f"reuse-start {instance_id}",
            )
            poll_instance_state(project, zone, instance_id, target_canonical="running", timeout=300)
            inst = get_instance(project, zone, instance_id)
            cstate = canonical_state(inst.status)
            started_in_reuse = True

        result["state"] = cstate
        result["instance_type"] = short_name(inst.machine_type)
        result["public_ip"] = first_external_ip(inst) or wait_for_public_ip(project, zone, instance_id, timeout=120)
        result["private_ip"] = first_internal_ip(inst)
        if inst.network_interfaces:
            result["vpc_id"] = short_name(inst.network_interfaces[0].network)
            if inst.network_interfaces[0].subnetwork:
                result["subnet_id"] = short_name(inst.network_interfaces[0].subnetwork)
        # Only emit canonical tag keys when their backing labels are
        # actually present on the live instance. Fabricating defaults
        # here would diverge from the AWS oracle reuse path, which
        # emits exactly what the API returned.
        actual_labels = dict(getattr(inst, "labels", {}) or {})
        derived_tags: dict[str, str] = {}
        if "isv_name" in actual_labels:
            derived_tags["Name"] = actual_labels["isv_name"]
        if "createdby" in actual_labels:
            derived_tags["CreatedBy"] = actual_labels["createdby"]
        result["tags"] = derived_tags

        # Derive the firewall handle from live state if possible — never
        # fabricate (rule #1 oracle-derivation parity). The gating flags
        # stay False either way, so this is purely informational.
        derived_fw = _find_ssh_firewall_for_instance(project, inst)
        if derived_fw:
            result["firewall_name"] = derived_fw
            result["security_group_id"] = derived_fw

        # Compute Engine has no managed key-pair store and no live
        # `KeyName` field on the instance record. There is no portable
        # counterpart to the AWS oracle signal, and the reviewed GCP
        # knowledge file requires emitting `result["key_name"] = None`
        # rather than synthesizing a basename / `<user>@reuse` token.
        # Local PEM identity flows through `key_file` alone.
        result["key_name"] = None

        if cstate != "running" or not result["public_ip"]:
            result["error"] = f"Instance {instance_id} is {cstate!r} or has no external IP"
            print(json.dumps(result, indent=2, default=str))
            return 1

        # The reuse branch must enforce the same readiness gate as the
        # create branch — consecutive-success stability, not first-SSH.
        # A reused VM whose sshd transiently flakes during the probe
        # would otherwise pass on a single success and surface as an
        # unstable readiness state to downstream validators.
        ssh_ok = wait_for_ssh_stable(
            host=result["public_ip"],
            user=ssh_user,
            key_file=key_file,
            consecutive=3,
            interval=10,
            max_attempts=36,
        )
        cloud_init_ok = False
        if ssh_ok:
            cloud_init_ok = wait_for_cloud_init(
                host=result["public_ip"],
                user=ssh_user,
                key_file=key_file,
                timeout_seconds=600,
            )
        result["ssh_ready"] = ssh_ok
        result["cloud_init_ok"] = cloud_init_ok
        # When the reuse branch just started the VM, both SSH stability
        # AND cloud-init completion are required to match the dedicated
        # start_instance step's success contract. Downstream validators
        # would otherwise race a guest whose cloud-init replay is still
        # rewriting authorized_keys / fstab. For a guest that was already
        # running when adopted, SSH stability alone is enough — cloud-init
        # only completes once per boot.
        if started_in_reuse:
            ready = ssh_ok and cloud_init_ok
        else:
            ready = ssh_ok or cloud_init_ok
        if ready:
            result["success"] = True
        else:
            result["error"] = (
                f"Instance {instance_id} is RUNNING but the reuse-path "
                "readiness gate (ssh stability + cloud-init completion) "
                "did not pass"
            )

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Launch a GPU VM on Compute Engine")
    parser.add_argument("--name", default="isv-test-gpu", help="Instance name")
    parser.add_argument(
        "--instance-type",
        required=True,
        help="Compute Engine machineType (e.g., g2-standard-8)",
    )
    parser.add_argument(
        "--region",
        required=True,
        help="GCP region or zone; if a region is given it's narrowed to <region>-a",
    )
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region narrowing)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--vpc-id", default=DEFAULT_NETWORK, help="Network short name")
    parser.add_argument("--subnet-id", default=None, help="Subnetwork short name")
    parser.add_argument(
        "--image-family",
        default=DEFAULT_IMAGE_FAMILY,
        help="GCP image family (resolved to a concrete image at runtime)",
    )
    parser.add_argument(
        "--image-project",
        default=None,
        help=(
            "GCP project hosting the image. When omitted: short-name "
            "--ami-id resolves in the operator project first (parameter-"
            "surface parity with the AWS oracle, where AMI IDs are "
            "account-scoped); --image-family resolves in the default "
            "project (where the canonical GPU image lives). Pass "
            "explicitly to override either fallback."
        ),
    )
    parser.add_argument(
        "--ami-id",
        default=None,
        help=(
            "Parameter-surface parity with the AWS oracle's --ami-id; "
            "if set, overrides --image-family lookup with a literal image"
        ),
    )
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME, help="Local SSH key label")
    parser.add_argument(
        "--firewall-name",
        default=DEFAULT_FIREWALL_NAME,
        help="SSH firewall rule name",
    )
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER, help="SSH username")
    args = parser.parse_args()

    # The provider config wires --subnet-id / --ami-id from settings that
    # default to the literal "none" sentinel so the orchestrator does not
    # collapse the flag/value pair. Treat the sentinel as "operator did
    # not supply" — the default
    # subnet for the resolved zone and the canonical image family take
    # over.
    if args.subnet_id == "none":
        args.subnet_id = None
    if args.ami_id == "none":
        args.ami_id = None
    if args.image_project == "none":
        args.image_project = None

    project = resolve_project(args.project)
    initial_zone = args.zone or narrow_region_to_zone(args.region)

    # Reuse-existing-instance branch (AWS oracle parity).
    reuse_instance = os.environ.get("GCP_VM_INSTANCE_ID")
    reuse_key = os.environ.get("GCP_VM_KEY_FILE")
    if reuse_instance and reuse_key:
        return _reuse_existing_instance(
            project=project,
            zone=initial_zone,
            instance_id=reuse_instance,
            key_file=reuse_key,
            ssh_user=args.ssh_user,
        )

    # Apply the RUN_ID suffix. Compute Engine names ARE the API IDs —
    # without the suffix, parallel runs collide on AlreadyExists during
    # create and /tmp/<key>.pem clobbers across sessions (name-collision
    # risk). The suffix lives at runtime, NOT in provider config (only
    # the team-letter belongs in config).
    instance_name = unique_suffix(args.name)
    firewall_name_suffixed = unique_suffix(args.firewall_name)
    key_name_suffixed = unique_suffix(args.key_name)

    # Multi-zone walk candidates. select_zones honors a single-zone pin
    # (full ``us-central1-a`` form) and otherwise queries the operator-
    # supplied region's live zones via the GCP API before iterating
    # preferred-in-region → other-in-region → cross-region
    # (zone_capacity_handling). Passing the resolved project lets the
    # helper query the regions API so a
    # valid region missing from PREFERRED_ZONES still walks its OWN
    # zones first.
    candidate_zones = select_zones(args.zone or args.region, project=project)

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        # Stays None until instances.insert ack; emitted only when an
        # API-accepted name exists (AWS oracle parity).
        "instance_id": None,
        "instance_type": args.instance_type,
        "region": args.region,
        "zone": initial_zone,
        "availability_zone": initial_zone,
        "project": project,
        "vpc_id": args.vpc_id,
        "subnet_id": args.subnet_id,
        # Compute Engine has no managed key-pair store; local PEM
        # identity flows through `key_file` end-to-end. The CLI accepts
        # `--key-name` for AWS-oracle invocation parity but the emitted
        # result is unconditionally None per the reviewed GCP knowledge.
        "key_name": None,
        # Producer-side sentinel defense: initialize to the canonical
        # sentinel so an exception BEFORE generate_ssh_keypair (e.g.,
        # resolve_image raising) still emits a non-empty value. The
        # consumer template in vm.yaml uses `default('none', true)`
        # (boolean mode) to collapse both undefined AND empty/sentinel
        # values to the same downstream arg; matching the producer side
        # keeps the contract tight under partial-failure JSON.
        "key_file": "none",
        "key_created": False,
        # Verified-reuse ownership for the instance itself. Stays False
        # until the instances.insert ack returns; teardown gates primary
        # and leaked-zone deletes on this so a pre-RUNNING failure
        # (e.g., image resolve) cannot make teardown destroy an
        # operator-supplied VM that this run never touched.
        "instance_created": False,
        "firewall_name": firewall_name_suffixed,
        "firewall_created": False,
        "security_group_id": firewall_name_suffixed,
        "ssh_user": args.ssh_user,
        "state": "",
        "public_ip": None,
        "private_ip": None,
        # Filled after resolve_image() — never echo the requested family.
        "ami_id": "",
        "tags": {},
        # leaked_zones flows into teardown's --leaked-zones arg so any
        # partial-create in a failed zone gets a second-chance delete
        # (zone_capacity_handling).
        "leaked_zones": [],
    }

    # Per-resource trackers for the cleanup-on-failure block.
    instance_created = False
    zone = initial_zone  # tracked separately so the walk can update it
    key_priv: str | None = None
    key_created = False
    firewall_created = False
    fw_name = firewall_name_suffixed

    try:
        # 0. Resolve image. Three operator-supplied shapes are honored
        # (operator scope wins):
        #   * Full self-link (``https://...`` or ``projects/<P>/global/
        #     images/<N>``) — pass through verbatim; Compute Engine
        #     accepts it as ``sourceImage``.
        #   * Short name OR family alias under ``--image-project`` —
        #     route through ``resolve_image`` which tries
        #     ``images.get`` then ``images.get_from_family``. This is
        #     the AWS-oracle parameter-surface parity case: the
        #     operator passes ``--ami-id <short>`` (mirroring AWS) and
        #     the stub resolves it inside the operator's chosen
        #     project, NOT a hardcoded vendor default.
        # ``--ami-id`` is treated as a literal-or-short hint; the family
        # alias branch uses ``--image-family`` so operators can supply
        # either without ambiguity.
        if args.ami_id:
            literal_image = args.ami_id
            is_full_path = literal_image.startswith(("projects/", "https://"))
            if is_full_path:
                resolved_source_image = literal_image
                result["ami_id"] = short_name(literal_image)
                result["ami_self_link"] = literal_image
                result["ami_name"] = short_name(literal_image)
            else:
                # Short name — operator-scope parameter-surface parity:
                # AMI IDs are account-scoped on AWS, so the AWS-oracle
                # invocation pattern (`--ami-id <short>`) MUST resolve in
                # the operator project on GCP. Try operator project
                # first; on NotFound (e.g., operator following a tutorial
                # that names a vendor-default image) fall back to the
                # vendor default. An explicit `--image-project` wins
                # over both.
                explicit_project = args.image_project
                operator_scope = explicit_project or project
                try:
                    image = resolve_image(operator_scope, literal_image)
                except gax.NotFound:
                    if explicit_project:
                        # Operator explicitly named the scope — do not
                        # silently substitute. Surface the error.
                        raise RuntimeError(
                            f"Image {literal_image!r} not found in project {explicit_project!r}"
                        ) from None
                    if operator_scope == DEFAULT_IMAGE_PROJECT:
                        # Already searched the default project; nothing
                        # more to try.
                        raise RuntimeError(f"Image {literal_image!r} not found in project {operator_scope!r}") from None
                    print(
                        f"  image {literal_image!r} not in operator project "
                        f"{operator_scope!r}; falling back to default project "
                        f"{DEFAULT_IMAGE_PROJECT!r}",
                        file=sys.stderr,
                    )
                    try:
                        image = resolve_image(DEFAULT_IMAGE_PROJECT, literal_image)
                    except gax.NotFound as e:
                        raise RuntimeError(
                            f"Image {literal_image!r} not found in operator project "
                            f"{operator_scope!r} or default project "
                            f"{DEFAULT_IMAGE_PROJECT!r}: {e}"
                        ) from e
                resolved_source_image = image.self_link
                result["ami_id"] = short_name(image.self_link)
                result["ami_name"] = image.name
                result["ami_self_link"] = image.self_link
        else:
            # Image-family lookup — the canonical GPU image lives in
            # the default project, so the family-default route reads from
            # there unless the operator overrides --image-project.
            family_scope = args.image_project or DEFAULT_IMAGE_PROJECT
            try:
                image = resolve_image(family_scope, args.image_family)
                resolved_source_image = image.self_link
                result["ami_id"] = short_name(image.self_link)
                result["ami_name"] = image.name
                result["ami_self_link"] = image.self_link
            except gax.NotFound as e:
                raise RuntimeError(f"Image {args.image_family!r} in project {family_scope!r} not found: {e}") from e

        # 1. Local SSH key pair (verified-reuse). Use the run-id-suffixed
        # name so /tmp/<base>-<run_id>.pem can't collide between sessions.
        # The tuple-unpack shape matches the drift-guard contract.
        key_priv, key_created = generate_ssh_keypair(key_name_suffixed)
        ssh_pubkey = read_ssh_pubkey(key_priv)
        result["key_file"] = key_priv
        result["key_created"] = key_created

        # 2. SSH firewall on the target network (verified-reuse).
        # Stamp-before-wait pattern — insert returns ``(name, op)``; the
        # caller stamps ``firewall_created`` BEFORE the wait so a
        # wait-side failure leaves the truthful flag for cleanup
        # (cleanup-tracker pattern). ``op is None`` when the helper
        # adopted a verified-reuse
        # existing rule, in which case ``firewall_created`` stays False.
        fw_name, fw_op = insert_ssh_firewall(
            project=project,
            name=firewall_name_suffixed,
            network_short=args.vpc_id,
        )
        if fw_op is not None:
            firewall_created = True
            result["firewall_created"] = True
            wait_for_global_op(project, fw_op.name, timeout=120)
        result["firewall_name"] = fw_name
        result["security_group_id"] = fw_name

        # 3. Build / insert with multi-zone walk on STOCKOUT.
        # Canonical tag projection happens at the boundary; the emitted
        # ``tags`` dict comes from a live readback further below. The
        # ``Name`` tag carries the same suffixed instance_name so
        # ``gcloud compute instances list --filter "labels.name~$RUN_ID"``
        # works for cross-resource grouping.
        canonical_tags = {"Name": instance_name, "CreatedBy": "isvtest"}
        labels = canonical_tags_to_labels(canonical_tags)

        instances_client = compute_v1.InstancesClient()
        last_error: Exception | None = None
        op = None
        op_name = ""
        for candidate_idx, candidate_zone in enumerate(candidate_zones, start=1):
            print(
                f"Inserting instance {instance_name} in "
                f"{project}/{candidate_zone} [{candidate_idx}/{len(candidate_zones)}]...",
                file=sys.stderr,
            )
            instance_resource = _build_instance_resource(
                project=project,
                zone=candidate_zone,
                name=instance_name,
                machine_type=args.instance_type,
                source_image=resolved_source_image,
                network_name=args.vpc_id,
                subnet_name=args.subnet_id,
                ssh_user=args.ssh_user,
                ssh_pubkey=ssh_pubkey,
                labels=labels,
            )
            try:
                op = instances_client.insert(
                    project=project,
                    zone=candidate_zone,
                    instance_resource=instance_resource,
                )
                # Stamp-before-wait: set the cleanup tracker AND
                # result['instance_id'] / result['zone'] IMMEDIATELY
                # after the insert ack, BEFORE the wait. A wait-side
                # failure then leaves the truthful identifier on disk
                # for teardown. result['instance_created'] is stamped
                # on the same tick so the teardown ownership flag
                # forwarded via vm.yaml stays in sync with the
                # in-process tracker driving cleanup-on-failure.
                instance_created = True
                zone = candidate_zone
                result["instance_id"] = instance_name
                result["instance_created"] = True
                result["zone"] = candidate_zone
                result["availability_zone"] = candidate_zone

                op_name = getattr(op, "name", None) or getattr(op, "operation", "")
                if op_name:
                    wait_for_zonal_op(project, candidate_zone, op_name, timeout=600)
                # Insert + DONE successful — break out of the walk.
                break
            except Exception as exc:
                # Async DONE-with-errors raises from wait_for_zonal_op.
                # Sync stockout raises from insert. is_zone_unavailable
                # covers all four shapes; treat non-zone errors as fatal.
                if not is_zone_unavailable(exc, op=op):
                    raise
                last_error = exc
                # Shape 2: clean up the partial async-insert before
                # moving to the next zone, so the failed zone doesn't
                # leak a phantom instance record.
                if instance_created:
                    print(
                        f"  zone {candidate_zone} unavailable; cleaning partial create",
                        file=sys.stderr,
                    )
                    cleaned = delete_failed_zonal_instance(project, candidate_zone, instance_name)
                    if not cleaned:
                        result["leaked_zones"].append(candidate_zone)
                    # Reset the per-zone tracker so the next iteration's
                    # insert ack stamps it fresh. Do NOT null
                    # result['instance_id'] / result['zone'] — the
                    # instance_name is deterministic across walker
                    # attempts (suffix is rolled once before the loop),
                    # so the stamped value remains a valid teardown
                    # target whether the walker succeeds later or
                    # exhausts every candidate. Clearing it broke the
                    # cleanup-provenance chain on full-walk exhaustion
                    # (rule #6) — teardown then lost the deterministic
                    # name and the leaked instance in zone A survived.
                    instance_created = False
                else:
                    # Sync stockout — no partial state to clean.
                    result["leaked_zones"].append(candidate_zone)
                print(f"  walking past {candidate_zone} (stockout-class)", file=sys.stderr)
                op = None
                op_name = ""
                continue
        else:
            # Exhausted every candidate — raise the most recent
            # zone-unavailable error so the operator sees the actual
            # cause rather than a generic "no zones tried" message.
            raise RuntimeError(
                f"Zone-walk exhausted ({len(candidate_zones)} candidates); last error: {last_error}"
            ) from last_error

        # 5. Poll canonical 'running'.
        print("Waiting for RUNNING status...", file=sys.stderr)
        result["state"] = poll_instance_state(
            project,
            zone,
            instance_name,
            target_canonical="running",
            timeout=300,
        )

        # 6. Re-read instance for IPs + label round-trip.
        inst = get_instance(project, zone, instance_name)
        result["public_ip"] = first_external_ip(inst) or wait_for_public_ip(project, zone, instance_name, timeout=120)
        if not result["public_ip"]:
            raise RuntimeError("Instance has no external IP after RUNNING (timed out polling)")
        result["private_ip"] = first_internal_ip(inst)
        result["vpc_id"] = short_name(inst.network_interfaces[0].network)
        if inst.network_interfaces[0].subnetwork:
            result["subnet_id"] = short_name(inst.network_interfaces[0].subnetwork)
        # Only emit canonical tag keys when the backing label is actually
        # present on the live instance. Falling back to the REQUESTED
        # values would fabricate a vacuous readback round-trip and mask
        # any regression in `canonical_tags_to_labels` projection (rule
        # #1 oracle-derivation parity; mirrors the reuse-branch shape).
        actual_labels = dict(getattr(inst, "labels", {}) or {})
        derived_tags: dict[str, str] = {}
        if "isv_name" in actual_labels:
            derived_tags["Name"] = actual_labels["isv_name"]
        if "createdby" in actual_labels:
            derived_tags["CreatedBy"] = actual_labels["createdby"]
        result["tags"] = derived_tags

        # 7. Best-effort readiness gate — SSH OR cloud-init counts as
        # success. Failing BOTH is the only honest reason to call launch
        # failed at this point.
        ssh_ok = wait_for_ssh(
            host=result["public_ip"],
            user=args.ssh_user,
            key_file=key_priv,
            max_attempts=20,
            interval=10,
        )
        cloud_init_ok = False
        if ssh_ok:
            cloud_init_ok = wait_for_cloud_init(
                host=result["public_ip"],
                user=args.ssh_user,
                key_file=key_priv,
                timeout_seconds=600,
            )
            # Compute Engine's guest-agent restarts sshd shortly after
            # cloud-init completes (refreshes authorized_keys / host
            # keys). Downstream validators (e.g. CloudInitCheck) connect
            # via paramiko immediately after this step returns and race
            # that restart, surfacing as "Error reading SSH protocol
            # banner: Connection reset by peer". Require 3 consecutive
            # SSH successes here so the post-cloud-init bounce is washed
            # out before we hand control back. Mirrors the reuse-branch
            # readiness gate.
            if cloud_init_ok:
                ssh_stable_ok = wait_for_ssh_stable(
                    host=result["public_ip"],
                    user=args.ssh_user,
                    key_file=key_priv,
                    consecutive=3,
                    interval=10,
                    max_attempts=24,
                )
                if not ssh_stable_ok:
                    print(
                        "  SSH did not stabilize after cloud-init; continuing on best-effort",
                        file=sys.stderr,
                    )
                result["ssh_stable"] = ssh_stable_ok
        result["ssh_ready"] = ssh_ok
        result["cloud_init_ok"] = cloud_init_ok

        if not (ssh_ok or cloud_init_ok):
            raise RuntimeError(
                "Launch reached RUNNING but neither SSH nor cloud-init became observable within the step timeout"
            )

        result["success"] = True
        print("Launch succeeded", file=sys.stderr)

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
        # Cleanup-on-failure — gate on per-resource trackers so a failed
        # verified-reuse adoption doesn't take a pre-existing shared
        # resource with it.
        try:
            if instance_created:
                print(
                    f"Cleanup-on-failure: deleting instance {instance_name}",
                    file=sys.stderr,
                )
                delete_with_retry(
                    _delete_instance_op,
                    project,
                    zone,
                    instance_name,
                    resource_desc=f"instance {instance_name}",
                )
            if firewall_created:
                print(
                    f"Cleanup-on-failure: deleting firewall {fw_name}",
                    file=sys.stderr,
                )
                delete_with_retry(
                    _delete_firewall_op,
                    project,
                    fw_name,
                    resource_desc=f"firewall {fw_name}",
                )
            if key_created and key_priv:
                delete_local_keypair(key_priv)
        except Exception as cleanup_exc:
            print(f"Cleanup-on-failure error: {cleanup_exc}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
