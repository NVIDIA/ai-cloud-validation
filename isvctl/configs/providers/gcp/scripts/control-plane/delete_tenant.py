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

"""Delete the run-owned tenant grouping resource (TagValue then its parent TagKey).

The AWS reference resolves and deletes the exact run-owned Resource Group. On GCP
the tenant is a TagValue plus its dedicated parent TagKey (create_tenant creates
one of each), and a TagValue must be deleted before its parent TagKey.

Cleanup is scoped exactly to the forwarded ``--group-name`` (the TagValue
namespaced name ``<project>/<key-short>/<value-short>``) — never a broad
discovery scan — and proves presence/absence by exact LIST readback, never by a
namespaced getter. The installed Resource Manager namespaced getters
(``get_namespaced_tag_key`` / ``get_namespaced_tag_value``) return
``PERMISSION_DENIED`` both when the exact resource is absent AND when it is merely
unreadable, so a getter cannot distinguish "gone" from "cannot read" and cannot
prove idempotent absence. A fully consumed, exactly scoped list can:

  * Parse ``--group-name`` into its exact project, TagKey short name, and TagValue
    short name.
  * Fully consume ``list_tag_keys(parent=projects/<project>)`` and match the
    dedicated TagKey by that exact project scope plus its short name. A no-match
    is trusted as absence only after a bounded absence-confirmation window of
    re-lists elapses — an accepted create whose blocking wait timed out can still
    materialize the TagKey after a single empty read, so one empty list is
    eventual consistency, never immediate terminal absence. A no-match sustained
    across the whole window PROVES the key (and therefore its value) are absent —
    idempotent success with nothing to delete.
  * If the key exists, fully consume ``list_tag_values(parent=<key.name>)`` and
    match the TagValue by that exact parent scope plus its short name.
  * Delete a matched TagKey/TagValue ONLY when the exact backend-assigned PERMANENT
    id create_tenant forwarded (``--tenant-id`` for the TagValue, ``--tenant-key-id``
    for the parent TagKey) matches the resident resource's ``name``. That permanent
    id, assigned by the backend at create, is unique to the exact resource this run
    created and cannot be forged, so it is the ONLY signal that authorizes deletion.
    The run-scoped short name and the run-ownership marker both derive from the
    shared ``RUN_ID`` and a same-run FOREIGN resource can carry BOTH, so neither a
    short-name nor a run-marker match is ever accepted as ownership proof. When
    create_tenant forwarded no permanent id (rendered as the ``none`` sentinel --
    an ambiguous create failure emitted only the deterministic coordinate handoff,
    not a confirmed id), ownership is UNPROVABLE: the resource at our coordinates is
    retained untouched and the step reports failure. It is never deleted by weaker
    evidence, because a same-run foreign resource could carry the identical marker
    AND squat the run-scoped short name at those exact coordinates. A resource whose
    permanent id does not match (or that cannot be authorized) is likewise never
    deleted; teardown never widens onto a resource this test did not create. Such a
    resource at OUR exact coordinates is an anomaly, not clean absence: a
    non-authorized TagKey at our key short name is retained untouched AND recorded
    as a cleanup failure (the run-owned tenant is unaccounted for), and a
    non-authorized TagValue is treated as an unexpected sibling that retains the
    parent and fails the step -- neither is ever silently reported as idempotent
    success.
  * Delete the exact owned TagValue first, then the exact parent TagKey; each
    async delete is waited to DONE under the bounded delete-retry envelope.
    NotFound from an exact delete is idempotent success.
  * Retain the parent TagKey (and report failure) when an unexpected sibling
    value still lives under the dedicated key — foreign state is never deleted.
  * Any list error — including ``PermissionDenied`` — stays visible in
    ``cleanup_errors`` and forces ``success=false``; it is NEVER read as absence.
  * A missing/empty upstream tenant name is rendered by the provider config as a
    non-empty sentinel so this step is invoked rather than skipped; it is treated
    as idempotent success (nothing exact to delete).

Usage:
    python3 delete_tenant.py --region us-central1 \
        --group-name my-project/isv-tenant-1a2b3c4d/isv-tenant-val-1a2b3c4d \
        --tenant-id tagValues/123456789012 \
        --tenant-key-id tagKeys/987654321098

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "resources_deleted": ["tagValues/123456789012", "tagKeys/987654321098"],
    "message": "Deleted tenant TagValue and parent TagKey"
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.errors import (
    classify_gcp_error,
    delete_with_retry,
    handle_gcp_errors,
    retry_idempotent_list,
)
from google.cloud import resourcemanager_v3

# Bounded blocking wait for each async delete LRO; well inside the 300s step
# timeout with room for the delete-retry envelope's backoffs.
_TAG_OP_TIMEOUT = 60  # seconds

# Bounded absence-confirmation window for the dedicated parent TagKey. A single
# no-match list does NOT prove absence when an accepted-but-still-outstanding
# create may materialize the key after our first read: create_tenant hands off a
# deterministic handle whenever its blocking wait timed out on an accepted create
# operation, and a timed-out .result() never cancels that operation, so the
# backend can create the TagKey AFTER teardown's first list read. That is an
# eventual-consistency create-then-read miss on Resource Manager, so a bounded
# read-after-write retry is required before a no-match can be trusted as absent.
# Re-list the exact project scope under a monotonic deadline (final read at or
# after it): an outstanding create surfaces the key (then we delete it), while a
# no-match sustained across the whole window is trustworthy idempotent absence.
# The happy teardown path matches on the FIRST read and never waits, and this
# window plus the two-op delete stack still fits inside the 300s step timeout.
_ABSENCE_CONFIRM_DEADLINE_SECONDS = 45
_ABSENCE_CONFIRM_INTERVAL = 15  # seconds between absence re-list reads

# Non-empty sentinel the provider config renders (via `default(..., true)`) for
# --group-name when create_tenant produced no tenant_name. It keeps the
# orchestrator from raising MissingStepRefError and silently skipping this
# teardown step; main() recognizes it and no-ops as idempotent success. Must
# match the literal in config/control-plane.yaml.
_MISSING_TENANT_SENTINEL = "__no_tenant__"

# Non-empty sentinel the provider config renders (via `default(..., true)`) for
# --tenant-id / --tenant-key-id when create_tenant produced no confirmed permanent
# id (an ambiguous create failure). An empty render would be dropped by the
# orchestrator's _render_args (which strips a rendered value whose arg carries a
# default() filter), leaving a bare flag that makes argparse exit before this step
# can emit JSON or clean anything. A real permanent id is ``tagValues/<n>`` or
# ``tagKeys/<n>``, so this literal can never collide with one. It means "permanent
# id unavailable": ownership cannot be proven by an unforgeable id, so the resource
# at our coordinates is retained and the step reports failure -- never deleted by a
# recomputable run marker or short-name match. Must match the literal in
# config/control-plane.yaml.
_UNAVAILABLE_ID_SENTINEL = "none"


def _forwarded_id(raw: str) -> str:
    """Normalize a forwarded permanent id, mapping the unavailable sentinel to empty.

    The provider config renders the non-empty ``none`` sentinel when create_tenant
    forwarded no confirmed permanent id (so an empty render is not dropped and does
    not break argparse). Collapsing that sentinel to the empty string here lets the
    ownership check treat "unavailable" uniformly: an empty ``expected_id`` proves
    no ownership and authorizes no deletion.
    """
    value = raw.strip()
    if value == _UNAVAILABLE_ID_SENTINEL:
        return ""
    return value


def _provisioned_by_run(resource: Any, expected_id: str) -> bool:
    """Return whether the forwarded permanent id authorizes deleting ``resource``.

    Ownership is proven ONLY by an EXACT match between the backend-assigned
    PERMANENT id create_tenant forwarded (``expected_id``) and the resident
    resource's ``name``. That permanent id, assigned by the backend at create time,
    is unique to the exact resource this run created and cannot be forged, so it is
    the only signal that distinguishes our resource from a same-run FOREIGN resource
    that squats our run-scoped short name -- and even carries the recomputable
    run-ownership marker (both derive from the shared ``RUN_ID``). A run marker,
    label, or short-name match is therefore NEVER accepted as ownership proof.

    When no permanent id was forwarded (``expected_id`` empty -- an ambiguous create
    failure emitted only the deterministic coordinate handoff, rendered by the
    config as the ``none`` sentinel), ownership is UNPROVABLE: return False so the
    caller retains the resource and reports failure. A possibly-committed tenant is
    thus left in place and surfaced, never silently deleted on forgeable evidence.
    """
    if not expected_id:
        return False
    return resource.name == expected_id


def _find_exact(list_fn: Any, parent: str, short_name: str, op_desc: str) -> tuple[Any, list[Any]]:
    """Fully consume ``list_fn(parent=parent)`` and return ``(exact_match, others)``.

    Absence is proven ONLY by a fully consumed list with no exact match. The
    installed Resource Manager namespaced GETTERS return ``PERMISSION_DENIED`` both
    when the exact resource is absent and when it is unreadable, so they cannot
    distinguish absence from a read failure; a list scoped to the exact parent can.
    ``parent`` is passed straight to the list call, so every returned item is
    already under that exact parent scope, and the short-name match then makes the
    hit exact (short names are unique within a parent). ``others`` collects every
    non-matching child so the caller can retain a parent that still owns an
    unexpected sibling value. The full pager is materialized inside
    ``retry_idempotent_list`` so every page is consumed under the retry envelope
    (a match on a later page is never missed, and a transient on a later-page
    fetch is retried instead of escaping the wrapper). Any list error (including
    ``PermissionDenied``) propagates so the caller surfaces it as a visible
    cleanup failure — a failed list is never absence.
    """
    match: Any = None
    others: list[Any] = []
    for item in retry_idempotent_list(list_fn, parent=parent, op_desc=op_desc):
        if item.short_name == short_name:
            match = item
        else:
            others.append(item)
    return match, others


def _find_tag_key_confirming_absence(list_fn: Any, parent: str, short_name: str, op_desc: str) -> Any:
    """Resolve the exact parent TagKey, re-listing before trusting a no-match as absence.

    A successful, exactly-scoped list with no matching short name is NOT
    immediately terminal absence here: teardown may be handed create_tenant's
    deterministic handle after an accepted create whose blocking wait TIMED OUT,
    and a timed-out ``.result()`` never cancels the operation, so the backend can
    still materialize this TagKey AFTER our first read (an eventual-consistency
    create-then-read miss). Re-list the same exact project scope under a bounded
    monotonic deadline: the moment an outstanding create surfaces the key we
    return it (the caller then deletes it and its child value), and only a
    no-match sustained across the whole window is returned as ``None`` — proven
    idempotent absence. A list ERROR is never swallowed here: it propagates from
    ``_find_exact`` to the caller, which records it as a visible cleanup failure
    rather than reading it as absence. The happy teardown path matches on the
    FIRST read and returns without ever sleeping.
    """
    deadline = time.monotonic() + _ABSENCE_CONFIRM_DEADLINE_SECONDS
    while True:
        match, _others = _find_exact(list_fn, parent, short_name, op_desc)
        if match is not None:
            return match
        if time.monotonic() >= deadline:
            return None
        time.sleep(_ABSENCE_CONFIRM_INTERVAL)


def _delete_tag_resource_waited(delete_fn: Any, name: str, resource_desc: str) -> bool:
    """Delete a tag resource and wait to DONE under the bounded retry envelope.

    ``delete_with_retry`` treats NotFound as the desired terminal state (idempotent
    success), retries transient / dependency-in-use conditions with backoff, and
    returns False only on an unrecoverable error. The waited callable blocks on the
    async op so a True return means the resource is observably gone, not merely
    that the delete was accepted.
    """

    def _delete_and_wait() -> None:
        delete_fn(name=name).result(timeout=_TAG_OP_TIMEOUT)

    return delete_with_retry(_delete_and_wait, resource_desc=resource_desc)


@handle_gcp_errors
def main() -> int:
    """Delete the exact run-owned TagValue + parent TagKey and print a teardown result."""
    parser = argparse.ArgumentParser(description="Delete a control-plane tenant (Resource Manager tags)")
    parser.add_argument("--group-name", required=True, help="Target TagValue namespaced name from create_tenant")
    parser.add_argument(
        "--tenant-id",
        default="",
        help="Exact permanent TagValue id (tagValues/<id>) from create_tenant; proves ownership. "
        "The 'none' sentinel means the id was unavailable (ambiguous create failure)",
    )
    parser.add_argument(
        "--tenant-key-id",
        default="",
        help="Exact permanent parent TagKey id (tagKeys/<id>) from create_tenant; proves ownership. "
        "The 'none' sentinel means the id was unavailable (ambiguous create failure)",
    )
    parser.add_argument("--region", default="", help="Accepted for contract parity; tags are global")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve the tenant (run teardown later)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        result["message"] = "Teardown skipped (--skip-destroy)"
        print(json.dumps(result, indent=2))
        return 0

    if args.group_name == _MISSING_TENANT_SENTINEL:
        # Setup emitted no tenant name (create_tenant failed or was skipped). The
        # config renders this sentinel so the orchestrator still invokes teardown
        # rather than silently skipping it. create_tenant's inline cleanup is
        # retry-aware and CONFIRMS each delete: it emits this empty sentinel only
        # when it confirmed no partial TagKey/TagValue remains, and otherwise hands
        # off the exact leftover as tenant_name. So the sentinel reliably means
        # nothing exact is left for this teardown to delete -> idempotent success.
        result["success"] = True
        result["message"] = "No tenant name from setup (sentinel); nothing to delete"
        print(json.dumps(result, indent=2))
        return 0

    parts = args.group_name.split("/")
    if len(parts) != 3 or not all(parts):
        result["error"] = f"malformed tenant name '{args.group_name}' (expected <project>/<key>/<value>)"
        print(json.dumps(result, indent=2))
        return 1
    project, key_short, value_short = parts
    key_parent = f"projects/{project}"

    # Exact backend-assigned permanent ids create_tenant forwarded. The config
    # renders the non-empty ``none`` sentinel (normalized to empty by _forwarded_id)
    # when an ambiguous create failure emitted only the coordinate handoff and no
    # confirmed id. A present id is the unforgeable proof of which resource THIS run
    # created; an empty id means ownership is UNPROVABLE, so the matched resource is
    # retained and the step fails -- never deleted on a recomputable run marker.
    expected_value_id = _forwarded_id(args.tenant_id)
    expected_key_id = _forwarded_id(args.tenant_key_id)

    tv_client = resourcemanager_v3.TagValuesClient()
    tk_client = resourcemanager_v3.TagKeysClient()

    resources_deleted: list[str] = []
    cleanup_errors: list[str] = []

    # Prove the dedicated parent TagKey's presence/absence by a fully consumed,
    # project-scoped list readback -- the namespaced getter cannot, because it
    # returns PERMISSION_DENIED for both an absent and an unreadable resource. A
    # list failure is a visible cleanup error, never absence. A no-match is only
    # trusted as absence after the bounded absence-confirmation window elapses,
    # so an accepted-but-still-outstanding create cannot materialize this TagKey
    # after a single empty read and leak past a first-empty-list success.
    key: Any = None
    try:
        key = _find_tag_key_confirming_absence(tk_client.list_tag_keys, key_parent, key_short, "list_tag_keys")
    except Exception as e:
        cleanup_errors.append(f"list TagKeys under {key_parent} failed: {classify_gcp_error(e)[1]}")

    if key is not None and not _provisioned_by_run(key, expected_key_id):
        # A TagKey occupies our run-scoped short name but deletion is NOT authorized.
        # Two invariants apply either way:
        #   1. NEVER widen teardown onto a resource whose ownership is unproven --
        #      leave this TagKey (and any value beneath it) untouched.
        #   2. This is an ANOMALY, not clean idempotent absence. Teardown was handed
        #      exact coordinates for a tenant THIS run created, yet the resource
        #      living there cannot be authorized for deletion. Silently reporting
        #      success would hide that the run-owned tenant is unaccounted for, so
        #      record a cleanup error and fail the step instead of passing.
        # Its name is deliberately not logged (we do not touch unauthorized state).
        if not expected_key_id:
            # create_tenant forwarded no permanent TagKey id (rendered as the ``none``
            # sentinel -- an ambiguous create failure). Ownership is UNPROVABLE: a
            # same-run foreign resource could carry the identical run marker AND squat
            # this short name, so deleting on that recomputable evidence risks
            # deleting foreign state. Retain the resource untouched and fail the step.
            cleanup_errors.append(
                f"TagKey occupies tenant short name '{key_short}' but create_tenant "
                "forwarded no permanent TagKey id (ambiguous create failure); ownership "
                "cannot be proven by an unforgeable id, so it is retained untouched and "
                "cleanup reports failure -- never deleted on a recomputable run marker"
            )
        else:
            cleanup_errors.append(
                f"foreign TagKey occupies tenant short name '{key_short}' "
                "(permanent id does not match the forwarded run-owned tenant id); "
                "retained untouched, run-owned tenant not confirmed deleted"
            )
        key = None

    if key is not None:
        # The key exists AND is run-owned -> prove its child TagValue by a
        # parent-scoped list under the exact permanent key name. A value-list
        # failure keeps the key (it may still own the value) and stays a visible
        # cleanup error.
        value: Any = None
        other_values: list[Any] = []
        value_list_ok = False
        try:
            value, other_values = _find_exact(tv_client.list_tag_values, key.name, value_short, "list_tag_values")
            value_list_ok = True
        except Exception as e:
            cleanup_errors.append(f"list TagValues under {key.name} failed: {classify_gcp_error(e)[1]}")

        if value is not None and not _provisioned_by_run(value, expected_value_id):
            # A TagValue squats our value short name under our owned key but deletion
            # is NOT authorized: either its permanent id does not match the forwarded
            # id (foreign squatter) or create_tenant forwarded no permanent TagValue
            # id (rendered as the ``none`` sentinel -- an ambiguous create failure, so
            # ownership is unprovable). A same-run foreign resource can carry the run
            # marker AND our short name, so only an exact forwarded-id match may
            # authorize deletion. Never delete an unauthorized resource: treat it as
            # an unexpected sibling so the parent is retained and the step reports the
            # anomaly rather than deleting on recomputable evidence (name never logged).
            other_values.append(value)
            value = None

        # ``child_absent`` is True only when a successful list proved the exact
        # owned value gone, or we deleted it here. An unproven list (read failed)
        # leaves it False so the parent key is retained on unproven state.
        child_absent = value_list_ok and value is None
        if value is not None:
            if _delete_tag_resource_waited(tv_client.delete_tag_value, value.name, f"TagValue {value.name}"):
                resources_deleted.append(value.name)
                child_absent = True
            else:
                cleanup_errors.append(f"delete TagValue {value.name} failed")

        # Delete the parent TagKey only once its child is confirmed gone AND no
        # unexpected sibling value remains under this dedicated key. An unexpected
        # child means something we do not own lives under the key: retain the
        # parent and report failure rather than force-deleting foreign state (its
        # names are never logged). A TagKey delete also cannot succeed while it
        # still owns a value.
        if value_list_ok and other_values:
            cleanup_errors.append(
                f"retained TagKey {key.name}: {len(other_values)} unexpected child TagValue(s) remain"
            )
        elif child_absent:
            if _delete_tag_resource_waited(tk_client.delete_tag_key, key.name, f"TagKey {key.name}"):
                resources_deleted.append(key.name)
            else:
                cleanup_errors.append(f"delete TagKey {key.name} failed")
        elif value_list_ok:
            cleanup_errors.append(f"skipped TagKey {key.name} delete because its TagValue is not confirmed absent")

    result["resources_deleted"] = resources_deleted
    result["success"] = not cleanup_errors
    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors
        result["error"] = classify_gcp_error(RuntimeError("; ".join(cleanup_errors)))[1]
        result["message"] = "Tenant cleanup incomplete"
    elif resources_deleted:
        result["message"] = "Deleted tenant TagValue and parent TagKey"
    else:
        result["message"] = "Tenant already absent (idempotent success)"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
