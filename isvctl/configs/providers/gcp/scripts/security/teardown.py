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

"""Security test teardown (safety-net sweep).

Each security test step already cleans up its own fixtures in a ``finally``
block, so this teardown is only a safety net for resources that a hard crash
left behind. It scans the security fixture families and deletes a resource ONLY
when dual-gate ownership holds:

  * the resource name carries this run's id token as a substring (every create
    step embeds it via ``unique_suffix`` -- ``RUN_ID[:8]`` for most names,
    ``RUN_ID[:6]`` plus a discriminator for tight-namespace service-account ids,
    and the alnum-cleaned id for the ``_``-joined custom-role id; the shortest
    embedding is 6 chars, so a 6-char token substring owns all of them), AND
  * for resource types that support labels (Compute disks/instances, GCS
    buckets), the resource also carries ``labels["created-by"] == "isvtest"``.

Service accounts and custom roles have no label surface, so they are gated on
the owned name-prefix family plus the run-id token alone.

A resource that matches a fixture name prefix but is MISSING the created-by
marker / run-id token belongs to another run (or to the operator) and is
counted into ``resources_skipped_unowned`` and NEVER deleted -- an honesty
signal that the sweep saw it but declined ownership.

Every fixture family is swept unconditionally; the per-fixture created flags the
provider config forwards are advisory only (a standalone ``--phase teardown``
after a crash runs in a process where the test steps never set them). The
dual-gate ownership check above is the sole guard against touching resources
this run did not create. Because that check owns a resource only when its name
embeds the run-id token, a standalone sweep REQUIRES the original run's
``RUN_ID``/``LS_RUN_ID`` to be re-exported; with no run id the sweep fails closed
(it would otherwise be a success-looking no-op that leaves preserved fixtures
behind) rather than reporting a hollow success.

Families swept:

  * Cloud KMS: CryptoKeys named ``isv-sec09-*`` / ``isv-sec11-*``. KMS keys and
    key rings cannot be hard-deleted, so only the key's versions are scheduled
    for destruction.
  * Compute disks ``isv-sec09-disk-*-<run>``, instances
    ``isv-sec11-*-vm-<run>``, and networks ``isv-sec11-*-vpc-<run>`` (every name
    is run-id suffixed via ``unique_suffix``, so the fixture word is an INFIX,
    not a suffix). An instance is deleted before its VPC, and a VPC's dependent
    subnetworks are deleted before the VPC itself.
  * Service accounts ``isv-sec04-*`` / ``isv-sec11-*`` and custom roles
    ``isv_sec04_*``.
  * GCS buckets ``isv-sec04-*`` / ``isv-sec11-*`` (objects emptied before the
    bucket is removed).

Every delete flows through ``common.errors.delete_with_retry`` (a NotFound is
the desired terminal state and counts as success; transient errors are
retried). Compute deletes (disk/instance/subnetwork/network) go through the
waited ``common.network`` helpers, which block on the returned async op until it
reaches DONE, so a ``resources_cleaned`` increment means the resource is
observably gone -- not merely that the delete call was accepted. The sweep is
best-effort: one failing delete does not abort the others.

Usage:
    python3 teardown.py --region us-central1 --project my-project
    python3 teardown.py --region us-central1 --skip-destroy

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "teardown",
    "resources_cleaned": 2,
    "resources_skipped_unowned": 0
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    delete_disk,
    delete_instance,
    delete_network,
    delete_subnetwork,
    list_subnetworks_for_network,
)
from common.service_account import delete_service_account
from google.api_core import exceptions as gax
from google.cloud import compute_v1, iam_admin_v1, kms_v1, storage

# Fixture name prefixes the security test steps stamp on the resources they
# create (mirrors the AWS reference's owned-prefix sets).
KMS_KEY_PREFIXES: tuple[str, ...] = ("isv-sec09-cmk", "isv-sec11-")
DISK_PREFIX = "isv-sec09-disk"
INSTANCE_PREFIX = "isv-sec11-"
NETWORK_PREFIX = "isv-sec11-"
SA_PREFIXES: tuple[str, ...] = ("isv-sec04-", "isv-sec11-")
ROLE_PREFIX = "isv_sec04_"
BUCKET_PREFIXES: tuple[str, ...] = ("isv-sec04-", "isv-sec11-")

# The created-by ownership label stamped on label-bearing fixtures (disks,
# instances, GCS buckets); the exact marker the create steps set.
CREATED_BY_LABEL = "created-by"
CREATED_BY_VALUE = "isvtest"


def _run_token() -> str:
    """Return this run's id token (alnum-normalized ``RUN_ID``/``LS_RUN_ID``, 6 chars).

    Create steps embed the run id in their fixture names in slightly different
    forms: ``unique_suffix`` appends ``-RUN_ID[:8]``; tight-namespace
    service-account ids use ``RUN_ID[:6]`` plus a per-invocation discriminator;
    the custom-role id joins an alnum-cleaned run hex with ``_``. The shortest
    embedding is 6 chars, so ownership is proven by this alnum-cleaned 6-char
    token appearing as a substring of the candidate name. Cleaning matches the
    role id's own normalization so the two never disagree. Returns an empty
    string when no run id is set (ad-hoc invocation); ``main`` treats that as a
    fail-closed condition rather than running a sweep that can prove no
    ownership (every ``_name_owned_by_run`` call would decline its resource).
    """
    sid = os.environ.get("RUN_ID") or os.environ.get("LS_RUN_ID") or ""
    cleaned = "".join(c for c in sid.lower() if c.isalnum())
    return cleaned[:6]


def _name_owned_by_run(name: str | None, run_token: str, prefixes: tuple[str, ...]) -> bool:
    """Return True iff ``name`` matches an owned prefix AND embeds this run's id token.

    The token is matched as a substring (not a suffix): most fixture names end
    with ``-RUN_ID[:8]``, but service-account ids carry a trailing discriminator
    and the custom-role id joins the run hex with ``_``, so an exact-suffix gate
    would miss them. A substring match owns all of them while still declining a
    parallel run's fixtures (a different run id never contains this token).
    """
    if not name or not run_token:
        return False
    if not name.startswith(prefixes):
        return False
    return run_token in name.lower()


def _has_created_by_label(labels: Any) -> bool:
    """Return True iff ``labels`` carries the ``created-by=isvtest`` ownership marker."""
    return dict(labels or {}).get(CREATED_BY_LABEL) == CREATED_BY_VALUE


def _sweep_kms_keys(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Schedule version destruction for owned CryptoKeys (keys are not hard-deletable).

    KMS has no flat key list: walk locations -> key rings -> crypto keys. A key
    is owned when its short name matches a fixture prefix and the run-id suffix.
    Keys carry no label surface, so ownership is name+suffix only.
    """
    errors: list[str] = []
    client = kms_v1.KeyManagementServiceClient()
    for location in client.list_locations(request={"name": f"projects/{project}"}).locations:
        try:
            for key_ring in client.list_key_rings(parent=location.name):
                for crypto_key in client.list_crypto_keys(parent=key_ring.name):
                    short = crypto_key.name.rsplit("/", 1)[-1]
                    if not short.startswith(KMS_KEY_PREFIXES):
                        continue
                    if not _name_owned_by_run(short, run_suffix, KMS_KEY_PREFIXES):
                        counters["skipped"] += 1
                        continue
                    if _destroy_key_versions(client, crypto_key.name, errors):
                        counters["cleaned"] += 1
        except (gax.PermissionDenied, gax.NotFound):
            # A location the caller cannot enumerate is not fatal -- keep walking.
            continue
    return errors


def _destroy_key_versions(
    client: kms_v1.KeyManagementServiceClient,
    key_name: str,
    errors: list[str],
) -> bool:
    """Schedule every ENABLED version of an owned key for destruction (best-effort).

    Returns True iff the key had its versions scheduled (or there were none left
    to schedule) without an unrecoverable error.
    """
    enabled_state = kms_v1.CryptoKeyVersion.CryptoKeyVersionState.ENABLED
    ok = True
    for version in client.list_crypto_key_versions(parent=key_name):
        if version.state != enabled_state:
            continue
        if not delete_with_retry(
            client.destroy_crypto_key_version,
            name=version.name,
            resource_desc=f"key version {version.name}",
        ):
            errors.append(f"destroy key version {version.name} failed")
            ok = False
    return ok


def _sweep_disks(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned Compute disks (dual-gate: run-id-suffixed name AND created-by label)."""
    errors: list[str] = []
    client = compute_v1.DisksClient()
    for zone_scope, scoped in client.aggregated_list(project=project):
        zone = zone_scope.rsplit("/", 1)[-1]
        for disk in getattr(scoped, "disks", None) or []:
            if not disk.name.startswith(DISK_PREFIX):
                continue
            owned = _name_owned_by_run(disk.name, run_suffix, (DISK_PREFIX,)) and _has_created_by_label(
                getattr(disk, "labels", None)
            )
            if not owned:
                counters["skipped"] += 1
                continue
            # delete_disk waits for the async zonal delete op to reach DONE, so a
            # cleaned count is only incremented after the disk is observably gone.
            if delete_with_retry(
                delete_disk,
                project,
                zone,
                disk.name,
                resource_desc=f"disk {disk.name}",
            ):
                counters["cleaned"] += 1
            else:
                errors.append(f"delete disk {disk.name} failed")
    return errors


def _sweep_instances(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned Compute instances. Must run BEFORE the VPC sweep (a VM pins its network)."""
    errors: list[str] = []
    client = compute_v1.InstancesClient()
    for zone_scope, scoped in client.aggregated_list(project=project):
        zone = zone_scope.rsplit("/", 1)[-1]
        for instance in getattr(scoped, "instances", None) or []:
            # Tenant probe VMs are named ``isv-sec11-<tenant>-vm-<run-id>`` via
            # unique_suffix, so the run id trails the ``-vm`` segment — match the
            # ``-vm-`` infix, not a ``-vm`` suffix (which never holds post-suffix).
            if not instance.name.startswith(INSTANCE_PREFIX) or "-vm-" not in instance.name:
                continue
            owned = _name_owned_by_run(instance.name, run_suffix, (INSTANCE_PREFIX,)) and _has_created_by_label(
                getattr(instance, "labels", None)
            )
            if not owned:
                counters["skipped"] += 1
                continue
            # delete_instance waits for the async zonal delete op to reach DONE,
            # so the cleaned count and the subsequent VPC sweep only proceed once
            # the VM (which pins its network) is observably gone.
            if delete_with_retry(
                delete_instance,
                project,
                zone,
                instance.name,
                resource_desc=f"instance {instance.name}",
            ):
                counters["cleaned"] += 1
            else:
                errors.append(f"delete instance {instance.name} failed")
    return errors


def _sweep_networks(project: str, region: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned VPC networks (and their dependent subnetworks first).

    Networks carry no label surface, so ownership is gated on name+suffix. Run
    AFTER the instance sweep so a dependent VM has been removed first.

    Tenant VPCs are created as ``unique_suffix("isv-sec11-<tenant>-vpc")``, so
    the run id trails the ``-vpc`` segment (``isv-sec11-a-vpc-<run-id>``). Match
    the ``-vpc-`` infix, not a ``-vpc`` suffix (which never holds post-suffix and
    would skip every owned VPC before the ownership gate even runs).

    A custom-mode VPC cannot be deleted while it still owns subnetworks, so the
    owned network's subnetworks in ``region`` are deleted first. Both deletes go
    through waited helpers (``delete_subnetwork`` / ``delete_network`` block on
    the async op until DONE) so a cleaned count is only incremented after the
    resource is observably gone.
    """
    errors: list[str] = []
    client = compute_v1.NetworksClient()
    for network in client.list(project=project):
        if not network.name.startswith(NETWORK_PREFIX) or "-vpc-" not in network.name:
            continue
        if not _name_owned_by_run(network.name, run_suffix, (NETWORK_PREFIX,)):
            counters["skipped"] += 1
            continue
        # Delete dependent subnetworks before the VPC (a custom-mode network pins
        # them). Subnetworks are regional; the run's --region is where the tenant
        # subnets were created. Skip the subnet pass only when no region is known.
        if region:
            for subnet in list_subnetworks_for_network(project, region, network.name):
                if delete_with_retry(
                    delete_subnetwork,
                    project,
                    region,
                    subnet.name,
                    resource_desc=f"subnetwork {subnet.name}",
                ):
                    counters["cleaned"] += 1
                else:
                    errors.append(f"delete subnetwork {subnet.name} failed")
        if delete_with_retry(
            delete_network,
            project,
            network.name,
            resource_desc=f"network {network.name}",
        ):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete network {network.name} failed")
    return errors


def _sweep_service_accounts(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned service accounts. SAs have no label surface -> gate on name+suffix."""
    errors: list[str] = []
    iam = iam_admin_v1.IAMClient()
    for sa in iam.list_service_accounts(name=f"projects/{project}"):
        local_part = sa.email.split("@", 1)[0]
        if not local_part.startswith(SA_PREFIXES):
            continue
        if not _name_owned_by_run(local_part, run_suffix, SA_PREFIXES):
            counters["skipped"] += 1
            continue
        if delete_service_account(sa.email):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete service account {sa.email} failed")
    return errors


def _sweep_custom_roles(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned project-level custom roles. Roles have no label surface."""
    errors: list[str] = []
    iam = iam_admin_v1.IAMClient()
    for role in iam.list_roles(request={"parent": f"projects/{project}"}):
        role_id = role.name.rsplit("/", 1)[-1]  # projects/<p>/roles/<id> -> <id>
        if not role_id.startswith(ROLE_PREFIX):
            continue
        if not _name_owned_by_run(role_id, run_suffix, (ROLE_PREFIX,)):
            counters["skipped"] += 1
            continue
        if delete_with_retry(
            iam.delete_role,
            name=role.name,
            resource_desc=f"custom role {role.name}",
        ):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete custom role {role.name} failed")
    return errors


def _sweep_buckets(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Empty and delete owned GCS buckets (dual-gate: run-id-suffixed name AND created-by label)."""
    errors: list[str] = []
    client = storage.Client(project=project)
    for bucket in client.list_buckets():
        if not bucket.name.startswith(BUCKET_PREFIXES):
            continue
        owned = _name_owned_by_run(bucket.name, run_suffix, BUCKET_PREFIXES) and _has_created_by_label(
            getattr(bucket, "labels", None)
        )
        if not owned:
            counters["skipped"] += 1
            continue
        try:
            # Empty the bucket first; a non-empty bucket rejects deletion.
            for blob in client.list_blobs(bucket.name):
                blob.delete()
        except gax.NotFound:
            counters["cleaned"] += 1
            continue
        except gax.GoogleAPICallError as e:
            errors.append(f"empty bucket {bucket.name}: {e}")
            continue
        if delete_with_retry(bucket.delete, resource_desc=f"bucket {bucket.name}"):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete bucket {bucket.name} failed")
    return errors


@handle_gcp_errors
def main() -> int:
    """Sweep leftover security test fixtures created by isvtest (dual-gate ownership)."""
    parser = argparse.ArgumentParser(description="Security test teardown (safety-net sweep)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    # Per-fixture created flags forwarded by the provider config as the strings
    # "true"/"false"; hints for which families a run actually populated.
    parser.add_argument("--kms-key-created", default="")
    parser.add_argument("--cmek-disk-created", default="")
    parser.add_argument("--sa-created", default="")
    parser.add_argument("--lp-role-created", default="")
    parser.add_argument("--lp-sa-created", default="")
    parser.add_argument("--lp-bucket-created", default="")
    parser.add_argument("--ti-sa-created", default="")
    parser.add_argument("--ti-vpc-created", default="")
    parser.add_argument("--ti-kms-created", default="")
    parser.add_argument("--ti-bucket-created", default="")
    parser.add_argument("--ti-instance-created", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "teardown",
        "resources_cleaned": 0,
        "resources_skipped_unowned": 0,
    }

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        print(json.dumps(result, indent=2))
        return 0

    # The per-fixture created flags the provider config forwards are advisory
    # only. A standalone `--phase teardown` after a crash runs in a process where
    # the test steps never executed, so each flag renders to "false"; gating the
    # sweep on them would make that recovery path a silent no-op that cleans
    # nothing. Every family is therefore swept unconditionally below, and the
    # dual-gate ownership check (owned name prefix + this run's id token, plus the
    # created-by label for label-bearing types) is the sole guard against
    # touching another run's or the operator's resources. The flags are recorded
    # only as a hint of what a same-process run reported creating.
    def _created(value: str) -> bool:
        return value == "true"

    reported_created = sorted(
        family
        for family, created in (
            ("kms_key", _created(args.kms_key_created) or _created(args.ti_kms_created)),
            ("cmek_disk", _created(args.cmek_disk_created)),
            (
                "service_account",
                _created(args.sa_created) or _created(args.lp_sa_created) or _created(args.ti_sa_created),
            ),
            ("custom_role", _created(args.lp_role_created)),
            ("bucket", _created(args.lp_bucket_created) or _created(args.ti_bucket_created)),
            ("network", _created(args.ti_vpc_created)),
            ("instance", _created(args.ti_instance_created)),
        )
        if created
    )
    print(
        f"teardown: run reported creating {reported_created or 'no'} fixture families; "
        "sweeping all families by dual-gate ownership",
        file=sys.stderr,
    )

    counters: dict[str, int] = {"cleaned": 0, "skipped": 0}
    cleanup_errors: list[str] = []
    run_token = _run_token()

    # Fail closed when no run id is available. Ownership is proven SOLELY by the
    # run-id token embedded in every fixture name, so with no token the dual-gate
    # check can own nothing and each family sweep declines every resource. A
    # standalone `--phase teardown` started in a fresh process (RUN_ID unset)
    # would otherwise finish as a success-looking no-op that silently leaves the
    # deliberately-preserved fixtures behind. Require the operator to re-export
    # the original run's RUN_ID (or LS_RUN_ID) so the sweep can actually own and
    # remove them (documented in docs/references/gcp.md).
    if not run_token:
        result["error"] = (
            "no run id available to prove fixture ownership: export RUN_ID (or "
            "LS_RUN_ID) set to the original run's id before a standalone "
            "`--phase teardown` sweep. Without it the dual-gate ownership check "
            "owns nothing and the sweep would be a success-looking no-op that "
            "leaves preserved fixtures behind."
        )
        print(json.dumps(result, indent=2))
        return 1

    try:
        project = resolve_project(args.project)
        # Dependency order: instances before their networks; everything else is
        # independent. Each family is best-effort -- a single failing delete
        # never aborts the remaining sweeps.
        cleanup_errors.extend(_sweep_kms_keys(project, run_token, counters))
        cleanup_errors.extend(_sweep_disks(project, run_token, counters))
        cleanup_errors.extend(_sweep_instances(project, run_token, counters))
        cleanup_errors.extend(_sweep_networks(project, args.region, run_token, counters))
        cleanup_errors.extend(_sweep_service_accounts(project, run_token, counters))
        cleanup_errors.extend(_sweep_custom_roles(project, run_token, counters))
        cleanup_errors.extend(_sweep_buckets(project, run_token, counters))
    except Exception as e:
        result["error"] = str(e)

    result["resources_cleaned"] = counters["cleaned"]
    result["resources_skipped_unowned"] = counters["skipped"]
    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors

    # Success when the sweep completed without an unrecoverable error: a clean
    # walk that found nothing to delete is a successful no-op.
    result["success"] = "error" not in result and not cleanup_errors
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
