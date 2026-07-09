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

"""Tear down the observability network and its run-owned dependencies (teardown).

Translates the AWS oracle's ``network/teardown`` onto Compute Engine. Ownership
is EXACT, never marker- or scan-based: each child resource is deleted ONLY by the
exact identifier forwarded from its creating step, gated on that step's created
flag:

  * instance     -> only ``--launch-instance`` in ``--instance-zone`` (plus any
                    ``--leaked-zones`` entry), gated on ``--instance-created``.
                    GCE identity is (zone, name), so a same-named instance in
                    another zone is preserved.
  * SSH firewall -> only exact ``--firewall-name``, gated on ``--firewall-created``.
  * subnetworks  -> only the ``--created-subnets`` allowlist (an adopted subnet
                    absent from the allowlist is preserved implicitly — no denylist).
  * network      -> deleted LAST, gated on ``--network-created``.

teardown_host normally removes the host + firewall first, so those deletes are
idempotent ``NotFound`` no-ops here — this step is defense-in-depth that drains
every run-owned dependency before deleting the VPC. Dependency-in-use (a resource
still draining) is retried with backoff via ``delete_with_retry``. The final
``success`` is the AND of every delete. ``--skip-destroy`` short-circuits to
success BEFORE resolving the project.

Emits:
    {"success": bool, "platform": "observability", "resources_destroyed": bool,
     "resources_deleted": [str, ...], "message": str, "error": str?}

AWS reference implementation:
    ../../aws/scripts/network/teardown.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import narrow_region_to_zone, resolve_project, wait_for_global_op, wait_for_zonal_op
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import delete_network, delete_subnetwork
from google.api_core import exceptions as gax
from google.cloud import compute_v1

_FALSY_SENTINELS = {"", "none", "null", "false"}

# Per-attempt op waits, bounded so delete_with_retry does not multiply budgets.
_TEARDOWN_INSTANCE_WAIT_S = 180
_TEARDOWN_FIREWALL_WAIT_S = 120
_TEARDOWN_SUBNET_WAIT_S = 180
_TEARDOWN_NETWORK_WAIT_S = 360


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
    """Tear down the observability network + dependencies and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Teardown the GCP observability network")
    parser.add_argument("--vpc-id", default="none", help="Compute Engine network name to delete")
    parser.add_argument("--region", required=True, help="GCP region (subnet + instance-zone derivation)")
    parser.add_argument("--network-created", default="false", help="Bool sentinel from create_network.network_created")
    parser.add_argument("--launch-instance", default="none", help="Host instance name (dependency drain)")
    parser.add_argument("--instance-created", default="false", help="Bool sentinel from launch_host.instance_created")
    parser.add_argument("--instance-zone", default="none", help="Zone the host landed in")
    parser.add_argument("--leaked-zones", default="none", help="Comma-separated zones with partial-insert leaks")
    parser.add_argument("--firewall-name", default="none", help="SSH firewall rule name (dependency drain)")
    parser.add_argument("--firewall-created", default="false", help="Bool sentinel from launch_host.firewall_created")
    parser.add_argument(
        "--created-subnets", default="none", help="Comma-separated run-created subnetwork names (delete allowlist)"
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve all resources (short-circuit)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "teardown_network",
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

    project = resolve_project(args.project)

    network_name = args.vpc_id if _truthy(args.vpc_id) else None
    instance_id = args.launch_instance if _truthy(args.launch_instance) else None
    fw_name = args.firewall_name if _truthy(args.firewall_name) else None
    instance_zone = args.instance_zone if _truthy(args.instance_zone) else narrow_region_to_zone(args.region)
    leaked_zones = _split_ids(args.leaked_zones)
    created_subnets = _split_ids(args.created_subnets)
    network_created = _truthy(args.network_created)
    instance_created = _truthy(args.instance_created)
    firewall_created = _truthy(args.firewall_created)

    instance_ok = True
    firewall_ok = True
    subnets_ok = True
    network_ok = True

    # 1a. Primary instance dependency (zonal) — exact id in its landed zone,
    # gated on instance_created.
    if instance_created and instance_id:
        print(f"Draining host {instance_id} in {instance_zone}...", file=sys.stderr)
        instance_ok = delete_with_retry(
            _delete_instance_op, project, instance_zone, instance_id, resource_desc=f"instance {instance_id}"
        )
        if instance_ok:
            result["resources_deleted"].append(f"instance:{instance_id}@{instance_zone}")

    # 1b. Leaked-zone reclaim — runs whenever a leaked zone is tracked, INDEPENDENT
    # of instance_created (the exhausted-zone-walk stockout path emits
    # instance_created=false yet a populated leaked_zones with the retained
    # deterministic instance name; gating reclaim on instance_created would orphan
    # the billable phantom). Each leaked zone is its own ownership signal; the
    # landed zone is skipped only when 1a already handled it.
    if instance_id:
        for leak_zone in leaked_zones:
            if instance_created and leak_zone == instance_zone:
                continue
            print(f"Leaked-zone drain: host {instance_id} in {leak_zone}", file=sys.stderr)
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

    # 2. SSH firewall (global) — exact name, gated on firewall_created.
    if firewall_created and fw_name:
        print(f"Draining firewall {fw_name}...", file=sys.stderr)
        firewall_ok = delete_with_retry(_delete_firewall_op, project, fw_name, resource_desc=f"firewall {fw_name}")
        if firewall_ok:
            result["resources_deleted"].append(f"firewall:{fw_name}")

    # 3. Subnetworks — ONLY the run-created allowlist (dependency-in-use retried).
    for subnet_name in created_subnets:
        print(f"Deleting subnetwork {subnet_name}...", file=sys.stderr)
        ok = delete_with_retry(
            delete_subnetwork,
            project,
            args.region,
            subnet_name,
            timeout=_TEARDOWN_SUBNET_WAIT_S,
            resource_desc=f"subnetwork {subnet_name}",
        )
        if ok:
            result["resources_deleted"].append(f"subnetwork:{subnet_name}")
        else:
            subnets_ok = False

    # 4. Network — deleted LAST, gated on network_created.
    if network_created and network_name:
        print(f"Deleting network {network_name}...", file=sys.stderr)
        network_ok = delete_with_retry(
            delete_network,
            project,
            network_name,
            timeout=_TEARDOWN_NETWORK_WAIT_S,
            resource_desc=f"network {network_name}",
        )
        if network_ok:
            result["resources_deleted"].append(f"network:{network_name}")
    elif network_name:
        print(
            f"  skipping network delete for {network_name} (network_created=false; adopted network preserved)",
            file=sys.stderr,
        )
        result.setdefault("warnings", []).append(f"network {network_name} preserved (adopted, not created)")

    result["success"] = bool(instance_ok and firewall_ok and subnets_ok and network_ok)
    result["resources_destroyed"] = result["success"]
    if result["success"]:
        result["message"] = f"Deleted {len(result['resources_deleted'])} observability network resource(s)"
    else:
        result["message"] = (
            f"Cleanup partial: instance_ok={instance_ok}, firewall_ok={firewall_ok}, "
            f"subnets_ok={subnets_ok}, network_ok={network_ok}"
        )
        result["error"] = "One or more observability network teardown operations failed"

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
