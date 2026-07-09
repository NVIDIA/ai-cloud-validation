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

"""Tear down the observability host, SSH firewall, and local key (teardown phase).

Translates the AWS oracle's bare-metal ``teardown`` (terminate instance + delete
managed key pair + delete security group) onto Compute Engine:

  * instance      -> ``InstancesClient.delete`` (zonal) in the forwarded landed
                     zone PLUS any ``--leaked-zones`` a stockout walk left behind;
                     GCE identity is (zone, name) so a same-named instance in
                     another zone is never touched. Gated on ``--instance-created``.
  * SSH firewall  -> ``FirewallsClient.delete`` (global), gated on the forwarded
                     ``--firewall-created`` ownership bit (a verified-reuse-adopted
                     rule is preserved).
  * local SSH key -> local PEM/.pub delete, gated on ``--key-created``.

``--delete-key-pair`` / ``--delete-security-group`` are the AWS-parity intent
switches; the forwarded ``*_created`` ownership bit remains the real gate. Each
delete is independent, ``NotFound`` is idempotent success, and the final
``success`` is the AND of every per-resource result. ``--skip-destroy``
short-circuits to success BEFORE resolving the project so an expired-credentials
environment still no-ops cleanly. The LOCAL private-key delete also runs before
``resolve_project`` — it needs no cloud access, so a missing / expired ADC must
never strand key material on disk; its result still folds into final success.

Emits:
    {"success": bool, "platform": "observability", "resources_destroyed": bool,
     "resources_deleted": [str, ...], "message": str, "error": str?}

AWS reference implementation:
    ../../aws/scripts/bare_metal/teardown.py (teardown_host reuses the bm stub)
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
    narrow_region_to_zone,
    resolve_project,
    wait_for_global_op,
    wait_for_zonal_op,
)
from common.errors import delete_with_retry, handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import compute_v1

_FALSY_SENTINELS = {"", "none", "null", "false"}

# Per-attempt op waits, bounded so delete_with_retry does not multiply budgets.
_TEARDOWN_INSTANCE_WAIT_S = 180
_TEARDOWN_FIREWALL_WAIT_S = 120


def _truthy(arg: str | None) -> bool:
    """Per-arg sentinel check: '' / 'none' / 'null' / 'false' are falsy."""
    if arg is None:
        return False
    return arg.strip().lower() not in _FALSY_SENTINELS


def _split_ids(raw: str | None) -> list[str]:
    """Split a comma-separated id arg, dropping falsy sentinels."""
    return [t.strip() for t in (raw or "").split(",") if t.strip() and t.strip().lower() not in _FALSY_SENTINELS]


def _delete_instance_op(project: str, zone: str, name: str) -> None:
    """Delete an instance and wait on the zonal op (NotFound idempotent)."""
    try:
        op = compute_v1.InstancesClient().delete(project=project, zone=zone, instance=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=_TEARDOWN_INSTANCE_WAIT_S)


def _delete_firewall_op(project: str, name: str) -> None:
    """Delete a firewall rule and wait on the global op (NotFound idempotent)."""
    try:
        op = compute_v1.FirewallsClient().delete(project=project, firewall=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_TEARDOWN_FIREWALL_WAIT_S)


@handle_gcp_errors
def main() -> int:
    """Tear down the observability host resources and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Teardown the GCP observability host")
    parser.add_argument("--instance-id", default="none", help="Host instance name to delete")
    parser.add_argument("--region", required=True, help="GCP region (instance-delete zone derivation fallback)")
    parser.add_argument("--zone", default="none", help="Zone the host landed in (overrides region derivation)")
    parser.add_argument("--instance-created", default="false", help="Bool sentinel from launch_host.instance_created")
    parser.add_argument("--leaked-zones", default="none", help="Comma-separated zones with partial-insert leaks")
    parser.add_argument("--firewall-name", default="none", help="SSH firewall rule name to delete")
    parser.add_argument("--firewall-created", default="false", help="Bool sentinel from launch_host.firewall_created")
    parser.add_argument("--key-file", default="none", help="Local SSH PEM path to delete (gated on --key-created)")
    parser.add_argument("--key-created", default="false", help="Bool sentinel from launch_host.key_created")
    parser.add_argument("--delete-key-pair", action="store_true", help="AWS-parity intent switch (key_created gates)")
    parser.add_argument(
        "--delete-security-group", action="store_true", help="AWS-parity intent switch (firewall_created gates)"
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve all resources (short-circuit)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "teardown_host",
        "resources_destroyed": False,
        "resources_deleted": [],
        "message": "",
    }

    # Preservation-mode short-circuits BEFORE any auth / client construction.
    if args.skip_destroy:
        result["success"] = True
        result["message"] = "Teardown skipped (--skip-destroy / GCP_OBSERVABILITY_SKIP_TEARDOWN=true)."
        print(json.dumps(result, indent=2))
        return 0

    # Parse every ownership input BEFORE touching the cloud. Local key cleanup
    # needs no cloud access, so it must not sit behind resolve_project(): a
    # missing / expired ADC raises there and escapes through @handle_gcp_errors,
    # which would strand sensitive private-key material on disk.
    instance_id = args.instance_id if _truthy(args.instance_id) else None
    fw_name = args.firewall_name if _truthy(args.firewall_name) else None
    key_file = args.key_file if _truthy(args.key_file) else None
    leaked_zones = _split_ids(args.leaked_zones)
    instance_created = _truthy(args.instance_created)
    firewall_created = _truthy(args.firewall_created)
    key_created = _truthy(args.key_created)

    instance_ok = True
    firewall_ok = True
    key_ok = True

    # 0. Local SSH key pair — LOCAL-ONLY, run before any cloud preflight so an
    # expired-ADC failure can never leave private-key material behind. Gated on
    # key_created and the AWS-parity intent switch; its result folds into the
    # final success below.
    if key_created and args.delete_key_pair and key_file:
        pub = key_file + ".pub"
        if os.path.exists(key_file) or os.path.exists(pub):
            print(f"Deleting local SSH key pair {key_file}...", file=sys.stderr)
            key_ok = delete_local_keypair(key_file)
            if key_ok:
                result["resources_deleted"].append(f"key:{key_file}")
        else:
            print(f"  local SSH key pair already absent: {key_file}", file=sys.stderr)
    elif key_file:
        print(f"  skipping SSH key delete (key_created=false): {key_file}", file=sys.stderr)

    # Cloud preflight + cloud deletes follow. If ADC is missing / expired,
    # resolve_project raises here and the decorator emits the structured error —
    # but the local key is already gone, so no sensitive material lingers.
    project = resolve_project(args.project)
    zone = args.zone if _truthy(args.zone) else narrow_region_to_zone(args.region)

    # 1a. Primary landed-zone delete — gated on instance_created.
    if instance_created and instance_id:
        print(f"Deleting host {instance_id} in {zone}...", file=sys.stderr)
        instance_ok = delete_with_retry(
            _delete_instance_op, project, zone, instance_id, resource_desc=f"instance {instance_id}"
        )
        if instance_ok:
            result["resources_deleted"].append(f"instance:{instance_id}@{zone}")
    elif instance_id and leaked_zones:
        print("instance_created=false; reclaiming leaked-zone phantom(s) only", file=sys.stderr)
    else:
        print("Skipping instance delete (instance_created=false or no id tracked)", file=sys.stderr)

    # 1b. Leaked-zone reclaim — runs whenever a leaked zone is tracked, INDEPENDENT
    # of instance_created. The exhausted-zone-walk stockout path emits
    # instance_created=false yet a populated leaked_zones with the retained
    # deterministic instance name, so gating this reclaim on instance_created would
    # orphan the billable phantom. Each leaked zone is its own ownership signal
    # (the run-id-suffixed name was accepted there); the landed zone is skipped
    # only when 1a already handled it.
    if instance_id:
        for leak_zone in leaked_zones:
            if instance_created and leak_zone == zone:
                continue
            print(f"Leaked-zone cleanup: host {instance_id} in {leak_zone}", file=sys.stderr)
            leak_ok = delete_with_retry(
                _delete_instance_op,
                project,
                leak_zone,
                instance_id,
                resource_desc=f"instance {instance_id}@{leak_zone}",
            )
            if leak_ok:
                result["resources_deleted"].append(f"instance:{instance_id}@{leak_zone}")
            else:
                instance_ok = False

    # 2. SSH firewall (global) — gated on the verified-reuse ownership bit and
    # the AWS-parity intent switch.
    if firewall_created and args.delete_security_group and fw_name:
        print(f"Deleting SSH firewall {fw_name}...", file=sys.stderr)
        firewall_ok = delete_with_retry(_delete_firewall_op, project, fw_name, resource_desc=f"firewall {fw_name}")
        if firewall_ok:
            result["resources_deleted"].append(f"firewall:{fw_name}")
    elif fw_name:
        print(
            f"  skipping firewall delete for {fw_name} (firewall_created=false; adopted rule preserved)",
            file=sys.stderr,
        )
        result.setdefault("warnings", []).append(f"firewall {fw_name} preserved (adopted, not created)")

    # Local key cleanup already ran in step 0 (before cloud preflight); its
    # key_ok result folds into the AND below alongside the cloud deletes.
    result["success"] = bool(instance_ok and firewall_ok and key_ok)
    result["resources_destroyed"] = result["success"]
    if result["success"]:
        result["message"] = f"Deleted {len(result['resources_deleted'])} observability host resource(s)"
    else:
        result["message"] = f"Cleanup partial: instance_ok={instance_ok}, firewall_ok={firewall_ok}, key_ok={key_ok}"
        result["error"] = "One or more observability host teardown operations failed"

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
