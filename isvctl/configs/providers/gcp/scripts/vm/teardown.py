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

"""Teardown a Compute Engine VM + verified-reuse companions.

Mirrors the AWS oracle's teardown.py shape (instance + SG + key pair),
translated to Compute Engine:

  * ``instances.delete`` is zonal; ``firewalls.delete`` is project-global.
  * Instance, firewall, and local key pair are ALL verified-reuse —
    destruction MUST gate on the ``_created: bool`` flags forwarded
    from launch_instance via ``{{steps.launch_instance.X}}`` (cleanup
    contract). For the instance this
    means a run started with ``GCP_VM_INSTANCE_ID`` / ``GCP_VM_KEY_FILE``
    against an operator-supplied long-lived VM emits
    ``instance_created=False`` and teardown skips both the primary and
    every leaked-zone delete so the adopted VM survives.
  * ``--skip-destroy`` short-circuits to success BEFORE resolving the
    project, so an expired-ADC environment can still no-op cleanly —
    preservation-mode flags MUST be evaluated before any auth-resolving
    helper.
  * NotFound on the cloud-side preflight is idempotent SUCCESS for the
    instance read, but must NOT short-circuit local PEM/.pub cleanup —
    NotFound-on-cloud-read idempotency must not short-circuit
    local-artifact cleanup.
  * Each cleanup helper returns ``bool``; the final ``success`` is the
    AND of every per-resource bool — helpers that return ``bool`` for
    batch-cleanup safety MUST surface the bool into
    ``result['success']``.

Sentinel handling: the provider config wires bool / path args with the
non-empty defaults ``'none'`` / ``'false'`` — forwarded inter-step
Jinja args MUST use ``| default(<NON-EMPTY sentinel>)``. The stub
treats ``none`` / ``null`` / ``""`` / ``false`` as "no artifact
tracked".
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
    delete_local_keypair,
    get_instance,
    narrow_region_to_zone,
    resolve_project,
    wait_for_global_op,
    wait_for_zonal_op,
)
from common.errors import delete_with_retry, handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import compute_v1

_FALSY_SENTINELS = {"", "none", "null", "false"}

# Bound the per-attempt wait so the 3-attempt delete_with_retry
# does not multiply zonal/global operation budgets into the
# enclosing teardown step timeout. The leaked-zones walk iterates over
# multiple zones with the same retry helper; without this bound, a
# transient throttle on a single zone could exhaust the step budget
# before later zones are even attempted. Firewall deletes have exceeded
# 120s in live GCP runs, so keep the global-op wait comfortably above
# that observed path while still inside the enclosing teardown budget.
_TEARDOWN_INSTANCE_WAIT_S = 180
_TEARDOWN_FIREWALL_WAIT_S = 300


def _truthy(arg: str | None) -> bool:
    """Per-arg sentinel check. Treats both "" / 'none' / 'null' / 'false' as falsy."""
    if arg is None:
        return False
    return arg.strip().lower() not in _FALSY_SENTINELS


def _delete_instance_op(project: str, zone: str, name: str) -> None:
    """Delete an instance and wait on the zonal op (NotFound is idempotent)."""
    try:
        op = compute_v1.InstancesClient().delete(project=project, zone=zone, instance=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", None) or getattr(op, "operation", "")
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=_TEARDOWN_INSTANCE_WAIT_S)


def _delete_firewall_op(project: str, name: str) -> None:
    """Delete a firewall rule and wait on the global op (NotFound is idempotent)."""
    try:
        op = compute_v1.FirewallsClient().delete(project=project, firewall=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", None) or getattr(op, "operation", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_TEARDOWN_FIREWALL_WAIT_S)


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown a Compute Engine VM + companions")
    parser.add_argument("--instance-id", required=True, help="Instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument(
        "--delete-key-pair",
        action="store_true",
        help="Delete the local SSH key pair if --key-created is truthy",
    )
    parser.add_argument(
        "--delete-security-group",
        action="store_true",
        help="Delete the SSH firewall rule if --firewall-created is truthy",
    )
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        help="Short-circuit to success (preserve cloud state) BEFORE resolving auth",
    )
    parser.add_argument("--firewall-name", default="none", help="Firewall rule name")
    parser.add_argument(
        "--firewall-created",
        default="false",
        help="Bool sentinel forwarded from launch_instance.firewall_created",
    )
    parser.add_argument(
        "--instance-created",
        default="false",
        help=(
            "Bool sentinel forwarded from launch_instance.instance_created. "
            "False skips both the primary and every leaked-zone instance "
            "delete so a verified-reuse adoption of an operator-supplied "
            "long-lived VM is never destroyed by this teardown."
        ),
    )
    parser.add_argument(
        "--key-file",
        default="none",
        help="Local SSH PEM path forwarded from launch_instance.key_file",
    )
    parser.add_argument(
        "--key-created",
        default="false",
        help="Bool sentinel forwarded from launch_instance.key_created",
    )
    parser.add_argument(
        "--leaked-zones",
        default="",
        help=(
            "Comma-separated zones the multi-zone walker accumulated "
            "partial-create leaks in. Teardown best-effort-deletes the "
            "instance in each before completing."
        ),
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "resources_destroyed": False,
        "deleted": {
            "instances": [],
            "firewall_rules": [],
            "key_files": [],
        },
        "resources_deleted": [],  # flat list shape matching AWS oracle / my-isv
        "message": "",
    }

    # Preservation-mode flag short-circuits BEFORE any cloud / auth call
    # so an expired-credentials environment still no-ops cleanly.
    if args.skip_destroy:
        result["success"] = True
        result["instance_id"] = args.instance_id
        result["message"] = f"Instance {args.instance_id} preserved (--skip-destroy); delete manually when done."
        print(json.dumps(result, indent=2, default=str))
        return 0

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    firewall_created = _truthy(args.firewall_created)
    key_created = _truthy(args.key_created)
    instance_created = _truthy(args.instance_created)
    instance_id = args.instance_id if _truthy(args.instance_id) else None
    fw_name = args.firewall_name if _truthy(args.firewall_name) else None
    key_file = args.key_file if _truthy(args.key_file) else None

    # Per-resource booleans surfaced into the final success.
    instance_ok = True
    firewall_ok = True
    key_ok = True

    # 1. Instance delete. Each preflight read is scoped narrow so a
    # transient probe error doesn't poison sibling cleanup blocks —
    # teardown preflight reads MUST NOT share an exception handler with
    # the cleanup blocks.
    #
    # The verified-reuse ownership gate (instance_created) bypasses
    # the preflight entirely — there is no observable difference
    # between "adopted, still present" and "we created and it's still
    # present", so we MUST trust the forwarded ownership bit rather
    # than the live state.
    instance_present = False
    if instance_id and not instance_created:
        print(
            f"Skipping instance delete for {instance_id} (instance_created=false; "
            "verified-reuse adoption — never destroy resources this run did not create)",
            file=sys.stderr,
        )
        result.setdefault("warnings", []).append(
            f"instance {instance_id} preserved (verified-reuse adoption: instance_created=false)"
        )
    elif instance_id:
        print(f"Deleting instance {instance_id} in {zone}...", file=sys.stderr)
        try:
            get_instance(project, zone, instance_id)
            instance_present = True
        except gax.NotFound:
            print(f"  instance {instance_id} already absent (NotFound)", file=sys.stderr)
            result.setdefault("warnings", []).append(f"instance {instance_id} not found at teardown — already deleted")
        except Exception as e:
            # Transient/API error during preflight: treat as present and let
            # delete_with_retry handle NotFound idempotency.
            print(f"  warn: instance preflight failed: {e}", file=sys.stderr)
            result.setdefault("warnings", []).append(f"instance preflight read failed: {e}")
            instance_present = True
    else:
        print("Skipping instance delete (no instance id was produced)", file=sys.stderr)

    if instance_present:
        assert instance_id is not None
        instance_ok = delete_with_retry(
            _delete_instance_op,
            project,
            zone,
            instance_id,
            resource_desc=f"instance {instance_id}",
        )
        if instance_ok:
            result["deleted"]["instances"].append(instance_id)
            result["resources_deleted"].append(f"instance:{instance_id}")

    # 1b. The multi-zone walker may have accumulated zones where a partial
    # async insert leaked; best-effort delete in each so phantom records do
    # not survive the run. Drop falsy sentinels so the per-zone delete loop
    # only walks real zone strings.
    leaked = [
        z.strip()
        for z in (args.leaked_zones or "").split(",")
        if z.strip() and z.strip().lower() not in _FALSY_SENTINELS
    ]
    if instance_id and not instance_created and leaked:
        # Verified-reuse adoption never invoked the multi-zone walker
        # (the walker runs only on the create path), so a leaked_zones
        # list arriving here is impossible under normal flow. If
        # something upstream wires it anyway, refuse to touch a
        # not-ours name in any zone.
        result.setdefault("warnings", []).append(
            f"leaked-zone cleanup skipped: instance_created=false (preserving adopted {instance_id})"
        )
    elif instance_id and instance_created:
        for leak_zone in leaked:
            if leak_zone == zone:
                continue  # already handled above
            print(f"Leaked-zone cleanup: instance {instance_id} in {leak_zone}", file=sys.stderr)
            leak_ok = delete_with_retry(
                _delete_instance_op,
                project,
                leak_zone,
                instance_id,
                resource_desc=f"instance {instance_id}@{leak_zone}",
            )
            # Leaked-zone failure surfaces into the aggregate success so the
            # operator sees an honest partial-cleanup verdict; the per-zone
            # delete is best-effort but its outcome is NOT swallowed.
            if not leak_ok:
                instance_ok = False
                result.setdefault("warnings", []).append(f"leaked-zone delete failed: {instance_id}@{leak_zone}")
            else:
                result["deleted"]["instances"].append(f"{instance_id}@{leak_zone}")
                result["resources_deleted"].append(f"instance:{instance_id}@{leak_zone}")
    elif leaked:
        result.setdefault("warnings", []).append(
            f"leaked zones ignored because no instance id was produced: {', '.join(leaked)}"
        )

    # 2. Firewall — gated on the verified-reuse flag forwarded by
    # launch_instance. NotFound is idempotent success; transient is
    # local-only (does not bypass key cleanup below).
    if args.delete_security_group:
        if firewall_created and fw_name:
            firewall_present = False
            try:
                compute_v1.FirewallsClient().get(project=project, firewall=fw_name)
                firewall_present = True
            except gax.NotFound:
                print(f"  firewall {fw_name} already absent (NotFound)", file=sys.stderr)
            except Exception as e:
                print(f"  warn: firewall preflight failed: {e}", file=sys.stderr)
                result.setdefault("warnings", []).append(f"firewall preflight read failed: {e}")
                firewall_present = True

            if firewall_present:
                print(f"Deleting firewall rule {fw_name}...", file=sys.stderr)
                firewall_ok = delete_with_retry(
                    _delete_firewall_op,
                    project,
                    fw_name,
                    resource_desc=f"firewall {fw_name}",
                )
                if firewall_ok:
                    result["deleted"]["firewall_rules"].append(fw_name)
                    result["resources_deleted"].append(f"firewall_rule:{fw_name}")
        else:
            print(
                "  skipping firewall delete (firewall_created=false or no name)",
                file=sys.stderr,
            )

    # 3. Local SSH key pair — gated on key_created. Runs regardless of
    # the instance preflight outcome (cloud-side NotFound must NOT
    # short-circuit local cleanup). ``delete_local_keypair`` handles
    # both halves of the pair so the .pub is removed even when the PEM
    # was already gone from a prior run.
    if args.delete_key_pair:
        if key_created and key_file:
            pub_path = key_file + ".pub"
            priv_present = os.path.exists(key_file)
            pub_present = os.path.exists(pub_path)
            if priv_present or pub_present:
                print(
                    f"Deleting local SSH key pair: {key_file} (priv={priv_present}, pub={pub_present})",
                    file=sys.stderr,
                )
                key_ok = delete_local_keypair(key_file)
                if key_ok:
                    if priv_present:
                        result["deleted"]["key_files"].append(key_file)
                        result["resources_deleted"].append(f"key_file:{key_file}")
                    if pub_present:
                        result["deleted"]["key_files"].append(pub_path)
                        result["resources_deleted"].append(f"key_file:{pub_path}")
            else:
                print(f"  local SSH key pair already absent: {key_file} + .pub", file=sys.stderr)
        else:
            print(
                "  skipping local key cleanup (key_created=false or no path)",
                file=sys.stderr,
            )

    # 4. Surface every per-resource bool into final success.
    result["success"] = bool(instance_ok and firewall_ok and key_ok)
    result["resources_destroyed"] = result["success"]
    if result["success"]:
        result["message"] = "Instance and verified-reuse companions deleted"
    else:
        result["message"] = f"Cleanup partial: instance_ok={instance_ok}, firewall_ok={firewall_ok}, key_ok={key_ok}"

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
