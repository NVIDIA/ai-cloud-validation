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

"""Tear down every resource the image-registry lifecycle created.

Mirrors the AWS reference's ``teardown.py`` shape (independent, idempotent
per-resource deletes; ``--skip-destroy`` short-circuit; success gated on the
conjunction of all deletes), translated to Compute Engine + Cloud Storage:

  * instance         -> ``InstancesClient.delete`` (zonal)
  * machine image    -> ``ImagesClient.delete`` (global)
  * disk objects     -> Cloud Storage object delete
  * storage bucket   -> Cloud Storage bucket delete (force-empties leftovers)
  * VPC firewall rule-> ``FirewallsClient.delete`` (global), gated on the
                        forwarded ``--firewall-created`` ownership bit
  * SSH key          -> local PEM/.pub delete, gated on ``--key-created``
  * service account  -> the launch step creates none (``instance_profile`` empty),
                        so nothing is deleted here

Every identifier is forwarded from its producing step via ``{{steps.<step>.<field>}}``
with a non-empty ``default('none', true)`` sentinel so an unresolved upstream
reference (an isolated teardown invocation where the upstream step did not run)
does not collapse the argv pair and skip the whole teardown. The stub treats ``none`` / ``null`` / ``""`` / ``false`` as "no
artifact tracked". Each delete has its own try/except; ``NotFound`` is idempotent
success; the final ``success`` is the AND of every per-resource result.

``--skip-destroy`` short-circuits to success BEFORE resolving the project or any
client so an expired-credentials environment can still no-op cleanly.

Required JSON output (suite ``teardown_checks`` group):
    {
        "success":           bool,
        "platform":          "image_registry",
        "resources_deleted": list[str],
        "message":           str,
        "error":             str,   # (optional) present when success is false
    }

Usage:
    python teardown.py --instance-id i --image-id img --disk-ids a,b --bucket-name bkt \\
        --key-name k --key-file /tmp/k.pem --security-group-id fw --instance-profile "" \\
        --region <region> [--skip-destroy]

AWS reference implementation:
    ../../aws/scripts/image-registry/teardown.py
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
from google.cloud import compute_v1, storage

_FALSY_SENTINELS = {"", "none", "null", "false"}

# Per-attempt op waits, bounded so delete_with_retry does not multiply budgets.
_TEARDOWN_INSTANCE_WAIT_S = 180
_TEARDOWN_IMAGE_WAIT_S = 300
_TEARDOWN_FIREWALL_WAIT_S = 300


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


def _delete_image_op(project: str, name: str) -> None:
    """Delete a machine image and wait on the global op (NotFound idempotent)."""
    try:
        op = compute_v1.ImagesClient().delete(project=project, image=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_TEARDOWN_IMAGE_WAIT_S)


def _delete_firewall_op(project: str, name: str) -> None:
    """Delete a firewall rule and wait on the global op (NotFound idempotent)."""
    try:
        op = compute_v1.FirewallsClient().delete(project=project, firewall=name)
    except gax.NotFound:
        return
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_TEARDOWN_FIREWALL_WAIT_S)


def _delete_storage(
    storage_client: storage.Client,
    bucket_name: str,
    disk_ids: list[str],
    result: dict[str, Any],
) -> bool:
    """Delete the named disk objects then the bucket. Already-gone is success."""
    try:
        bucket = storage_client.get_bucket(bucket_name)
    except gax.NotFound:
        print(f"  bucket gs://{bucket_name} already absent", file=sys.stderr)
        return True
    except Exception as e:  # google.cloud.exceptions.NotFound subclasses vary
        if "404" in str(e) or "not found" in str(e).lower():
            print(f"  bucket gs://{bucket_name} already absent", file=sys.stderr)
            return True
        print(f"  warn: bucket lookup failed: {e}", file=sys.stderr)
        return False

    ok = True
    for obj in disk_ids:
        try:
            bucket.blob(obj).delete()
            result["resources_deleted"].append(f"disk_object:{obj}")
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                continue
            print(f"  warn: object delete {obj} failed: {e}", file=sys.stderr)
            ok = False
    try:
        bucket.delete(force=True)
        result["resources_deleted"].append(f"bucket:{bucket_name}")
    except Exception as e:
        if "404" in str(e) or "not found" in str(e).lower():
            return ok
        print(f"  warn: bucket delete failed: {e}", file=sys.stderr)
        ok = False
    return ok


@handle_gcp_errors
def main() -> int:
    """Tear down image-registry resources and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Teardown image-registry resources")
    parser.add_argument("--instance-id", default="none", help="Instance name to delete")
    parser.add_argument("--image-id", default="none", help="Machine image name to delete")
    parser.add_argument("--disk-ids", default="none", help="Comma-separated GCS disk-object keys to delete")
    parser.add_argument("--bucket-name", default="none", help="Cloud Storage bucket to delete")
    parser.add_argument("--key-name", default="none", help="SSH key resource name (informational)")
    parser.add_argument("--key-file", default="none", help="Local SSH PEM path to delete (gated on --key-created)")
    parser.add_argument("--key-created", default="false", help="Bool sentinel from launch_instance.key_created")
    parser.add_argument("--security-group-id", default="none", help="VPC firewall rule name to delete")
    parser.add_argument(
        "--firewall-created", default="false", help="Bool sentinel from launch_instance.firewall_created"
    )
    parser.add_argument("--instance-profile", default="none", help="Attached service-account email (none created)")
    parser.add_argument("--leaked-zones", default="none", help="Comma-separated zones with partial-insert leaks")
    parser.add_argument("--region", required=True, help="GCP region (instance-delete zone derivation)")
    parser.add_argument("--zone", default=None, help="GCP zone the instance landed in (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve all resources (short-circuit)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "resources_deleted": [],
        "message": "",
    }

    # Preservation-mode short-circuits BEFORE any auth / client construction so
    # an expired-credentials environment still no-ops cleanly.
    if args.skip_destroy:
        result["success"] = True
        result["message"] = "Teardown skipped (--skip-destroy); delete resources manually when done."
        print(json.dumps(result, indent=2))
        return 0

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    instance_id = args.instance_id if _truthy(args.instance_id) else None
    image_id = args.image_id if _truthy(args.image_id) else None
    bucket_name = args.bucket_name if _truthy(args.bucket_name) else None
    fw_name = args.security_group_id if _truthy(args.security_group_id) else None
    # The SSH "key resource" is identified by --key-name (contract arg); the
    # deletable artifact is the local PEM/.pub pair at --key-file.
    key_name = args.key_name if _truthy(args.key_name) else None
    key_file = args.key_file if _truthy(args.key_file) else None
    disk_ids = _split_ids(args.disk_ids)
    leaked_zones = _split_ids(args.leaked_zones)
    firewall_created = _truthy(args.firewall_created)
    key_created = _truthy(args.key_created)
    instance_profile = args.instance_profile if _truthy(args.instance_profile) else None

    instance_ok = True
    image_ok = True
    storage_ok = True
    firewall_ok = True
    key_ok = True

    # 1. Instance (zonal). Best-effort delete in the landed zone + any leaked zones.
    if instance_id:
        print(f"Deleting instance {instance_id} in {zone}...", file=sys.stderr)
        instance_ok = delete_with_retry(
            _delete_instance_op, project, zone, instance_id, resource_desc=f"instance {instance_id}"
        )
        if instance_ok:
            result["resources_deleted"].append(f"instance:{instance_id}")
        for leak_zone in leaked_zones:
            if leak_zone == zone:
                continue
            print(f"Leaked-zone cleanup: instance {instance_id} in {leak_zone}", file=sys.stderr)
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
    else:
        print("Skipping instance delete (no instance id tracked)", file=sys.stderr)

    # 2. Machine image (global).
    if image_id:
        print(f"Deleting machine image {image_id}...", file=sys.stderr)
        image_ok = delete_with_retry(_delete_image_op, project, image_id, resource_desc=f"image {image_id}")
        if image_ok:
            result["resources_deleted"].append(f"image:{image_id}")
    else:
        print("Skipping image delete (no image id tracked)", file=sys.stderr)

    # 3. Cloud Storage disk objects + bucket.
    if bucket_name:
        print(f"Deleting bucket gs://{bucket_name} ({len(disk_ids)} object(s))...", file=sys.stderr)
        storage_ok = _delete_storage(storage.Client(project=project), bucket_name, disk_ids, result)
    elif disk_ids:
        result.setdefault("warnings", []).append(f"disk objects ignored: no bucket tracked ({disk_ids})")

    # 4. VPC firewall rule — gated on the verified-reuse ownership bit.
    if fw_name and firewall_created:
        print(f"Deleting firewall rule {fw_name}...", file=sys.stderr)
        firewall_ok = delete_with_retry(_delete_firewall_op, project, fw_name, resource_desc=f"firewall {fw_name}")
        if firewall_ok:
            result["resources_deleted"].append(f"firewall_rule:{fw_name}")
    elif fw_name:
        print(
            f"  skipping firewall delete for {fw_name} (firewall_created=false; verified-reuse adoption)",
            file=sys.stderr,
        )
        result.setdefault("warnings", []).append(f"firewall {fw_name} preserved (adopted, not created)")

    # 5. SSH key (--key-name) — the deletable artifact is the local PEM/.pub pair
    # at --key-file. Gated on key_created so a verified-reuse adopted key is kept.
    if key_created and key_file:
        pub = key_file + ".pub"
        if os.path.exists(key_file) or os.path.exists(pub):
            print(f"Deleting SSH key {key_name or key_file} (local pair {key_file})...", file=sys.stderr)
            key_ok = delete_local_keypair(key_file)
            if key_ok:
                result["resources_deleted"].append(f"key:{key_name or key_file}")
        else:
            print(f"  SSH key {key_name or key_file} local pair already absent: {key_file}", file=sys.stderr)
    elif key_name or key_file:
        print(f"  skipping SSH key delete (key_created=false): {key_name or key_file}", file=sys.stderr)

    # 6. Service account / instance profile — the launch step creates none, so
    # there is nothing this run owns to delete (never delete an SA we did not
    # create — cleanup-provenance safety).
    if instance_profile:
        result.setdefault("warnings", []).append(
            f"instance_profile {instance_profile} not deleted (no SA created by this run)"
        )

    result["success"] = bool(instance_ok and image_ok and storage_ok and firewall_ok and key_ok)
    if result["success"]:
        result["message"] = f"Deleted {len(result['resources_deleted'])} image-registry resource(s)"
    else:
        result["message"] = (
            f"Cleanup partial: instance_ok={instance_ok}, image_ok={image_ok}, "
            f"storage_ok={storage_ok}, firewall_ok={firewall_ok}, key_ok={key_ok}"
        )

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
