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

"""Create a tenant grouping resource (Resource Manager TagKey + TagValue).

The AWS reference models a tenant as a temporary Resource Group. On GCP the
closest run-owned grouping resource is a Resource Manager TagValue, which groups
resources for policy and is a child of a TagKey. Both expose a permanent ``name``
and an output-only ``namespaced_name``.

This step creates one run-scoped project-parented TagKey, waits for its
operation, then creates one run-scoped TagValue under it and waits for
completion. It emits:

  * ``tenant_name``  = ``TagValue.namespaced_name`` (the human-readable,
    forwarded-to-teardown handle: ``<project>/<key-short>/<value-short>``)
  * ``tenant_id``    = ``TagValue.name`` (the permanent ``tagValues/<id>`` id)

Names carry a run-id suffix so parallel runs never collide, and every created
tag also carries TWO ownership markers in its ``description``: a run-scoped marker
(see ``_run_marker``) that delete_tenant recomputes across process boundaries, and
an INVOCATION-specific marker (see ``_invocation_marker``) that only THIS create
process can produce. A run-scoped short-name collision is NOT proof of same-run
ownership -- the colliding resource may belong to another actor, and even a
same-run foreign resource can carry the run marker -- so the create-side readbacks
match on the invocation marker (which a foreign resource cannot forge) and
failures are separated into DEFINITE and AMBIGUOUS outcomes:

  * A genuine ``AlreadyExists`` OR ``Aborted`` on create (the HTTP 409 conflict
    class) is a hard create failure (matching the sister object-store stub's
    bucket ``Conflict`` handling): the colliding TagKey/TagValue is never adopted,
    and delete_tenant is never pointed at a resource this run did not create. On a
    TagKey conflict the teardown handoff clears to the empty sentinel; on a
    TagValue conflict under a freshly-created key the owned parent TagKey is
    best-effort deleted and, if that delete is unconfirmed, a PARENT-ONLY handle is
    emitted so teardown retries our TagKey while a reserved value segment keeps it
    from ever targeting the foreign colliding value.
  * A definite pre-commit client rejection (permission / not-found) created
    nothing, so the handoff clears to the empty sentinel (for the parent) or cleans
    the owned parent (for the child).
  * An AMBIGUOUS transport drop or 5xx / 429 / timeout may have committed the
    resource before the response was lost. Absence is never assumed: a bounded,
    exact-scope readback matched on BOTH the run-scoped short name AND this
    invocation's marker resolves only a resource THIS invocation created (a foreign
    squatter -- even one carrying the shared run marker -- is never resolved,
    adopted, or deleted). If that readback POSITIVELY resolves the owned resource,
    the create genuinely committed despite the failed wait, so the verdict is
    grounded in the API state: the tenant is reported created (the owned key is
    recovered and the child create continues; a recovered value is emitted as a
    clean success), never as a spurious wait failure. Only when the readback cannot
    confirm the resource is the wait failure terminal, and a non-empty teardown
    handoff is then preserved until child-before-parent absence is confirmed.

Each create separates SUBMISSION (the ``create_*`` RPC that returns an operation)
from the blocking WAIT (``operation.result()``). Only after a submission is
accepted is the deterministic recoverable handle
(``<project>/<key-short>/<value-short>``) stamped into ``tenant_name``, BEFORE the
blocking wait, so a genuine conflict (which owns nothing) never hands teardown a
predicted name this run did not create. If the backend accepts a create but our
wait -- and the follow-up marker-scoped readback -- cannot prove absence, teardown
still receives the exact project/key/value coordinates to resolve and delete any
server-created tag metadata this run DID create; a failed or empty readback must
never collapse to the empty sentinel that teardown reads as confirmed absence. If a
create/wait fails after the parent TagKey exists, the partial TagValue (if any) is
cleaned up before its parent TagKey; inline cleanup is retry-aware and confirms each
delete. Only a readback that positively confirms nothing remains clears the handle
to the empty sentinel; otherwise the exact resolved (or deterministic) handle is
emitted as ``tenant_name`` so teardown (delete_tenant) retries the leftover. On a
clean create the emitted TagValue is confirmed by a marker-scoped readback so the
forwarded handoff reflects what the API actually returns.

Usage:
    python3 create_tenant.py --region us-central1

On a clean create the emitted result also forwards the two backend-assigned
PERMANENT ids -- ``tenant_id`` (the ``tagValues/<id>``) and ``tenant_key_id``
(the parent ``tagKeys/<id>``). delete_tenant matches on these exact ids so a
same-run foreign resource that squats our run-scoped short name AND carries the
recomputable run marker (delete_tenant cannot recompute the per-invocation nonce
across process boundaries) is never deleted: the backend-assigned permanent id is
unique to the exact resource THIS run created and cannot be forged. On a create
FAILURE each permanent id is forwarded whenever it is KNOWN -- ``tenant_key_id``
as soon as the parent TagKey is confirmed created, and ``tenant_id`` whenever a
marker-scoped readback positively resolves the committed child -- so a recoverable
partial allocation hands teardown the exact ids it needs to PROVE ownership and
delete the leftover rather than leaving an owned TagKey/TagValue teardown refuses
to touch. An id stays empty (rendered as the ``none`` sentinel) ONLY when it is
genuinely unknown: the resource was never created, was confirmed already deleted
inline, or could not be resolved by readback. In that case delete_tenant retains
any resource at those coordinates and reports failure rather than deleting on a
recomputable run marker.

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "tenant_name": "my-project/isv-tenant-1a2b3c4d/isv-tenant-val-1a2b3c4d",
    "tenant_id": "tagValues/123456789012",
    "tenant_key_id": "tagKeys/987654321098"
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import (
    classify_gcp_error,
    delete_with_retry,
    handle_gcp_errors,
    is_transport_disconnect,
    retry_idempotent_list,
)
from google.api_core import exceptions as gax
from google.cloud import resourcemanager_v3

# Resource Manager tag create/delete operations are async LROs. Cap the blocking
# wait so a hung backend surfaces well inside the provider-config step timeout
# (300s) rather than hanging the whole run.
_TAG_OP_TIMEOUT = 90  # seconds

# Bounded readback poll for the AMBIGUOUS create-failure recovery paths. A create
# whose wait fails with a transport drop / 5xx / 429 / timeout may have COMMITTED
# the TagKey/TagValue server-side with DELAYED VISIBILITY: the tag is not yet
# returned by a list issued immediately after the failed wait, but becomes visible
# a short time later. A single-shot readback right after the error would MISS such
# a resource and mis-decide a genuine (delayed-visible) create as a wait failure --
# the verdict would be produced without letting the API's eventual state settle. So
# the marker-scoped readback POLLS a bounded number of times with escalating
# backoff, deciding the verdict from the API state once visibility settles instead
# of from a single premature miss. The budget stays well inside the provider-config
# step timeout (300s) even when both the key and value recovery paths poll.
_READBACK_ATTEMPTS = 5
_READBACK_BACKOFF = 2.0  # seconds, escalated per attempt (waits 2s, 4s, 6s, 8s between tries)

_KEY_DESCRIPTION = "ISV control-plane tenant-lifecycle test key"
_VALUE_DESCRIPTION = "ISV control-plane tenant-lifecycle test value"

# Reserved TagValue short name for a PARENT-ONLY teardown handoff. Used only in
# the exceptional path where this invocation created the parent TagKey but the
# child TagValue create hit AlreadyExists -- meaning a value this run does NOT own
# already occupies our value short name under our freshly-created key. We own the
# parent TagKey and want teardown to retry deleting it, but must NOT direct
# teardown to delete that foreign value (which shares our value short name).
# delete_tenant deletes only the value whose short name equals the handle's value
# segment and PROTECTS every other ("unexpected sibling") value by retaining the
# parent; a value segment that matches no real value therefore makes teardown
# (a) never delete the foreign value and (b) delete our parent TagKey only once it
# owns no children. This literal carries no RUN_ID and uses characters
# unique_suffix never emits, so it can never equal a real run-owned value short
# name. It needs no special-casing in delete_tenant -- the no-match is handled by
# the existing exact-scope readback path.
_PARENT_ONLY_VALUE = "__parent_only__"

# Per-INVOCATION ownership nonce, generated once when this create_tenant process
# starts. The run marker (RUN_ID) below and ``unique_suffix`` short names are both
# DETERMINISTIC from RUN_ID, so within a single suite run they are NOT unique to
# THIS create invocation: a foreign tag pre-seeded in the same run can carry the
# identical run marker AND squat the identical run-scoped short name. The
# create-side readback that resolves a possibly-committed resource (ambiguous
# create failure) or confirms a clean create must therefore match on something a
# same-run foreign squatter cannot forge -- this fresh per-process nonce, stamped
# into every tag THIS invocation creates. Matching on it means the create paths
# resolve, adopt, and clean up ONLY resources this exact invocation created, never
# a same-run foreign resource that merely shares the run marker and short name.
_INVOCATION_ID = uuid.uuid4().hex


def _run_marker() -> str:
    """Return the run-scoped ownership marker stamped into every created tag.

    The marker carries the suite ``RUN_ID`` and is embedded in each TagKey /
    TagValue ``description`` at create time. delete_tenant runs in the same suite
    run and recomputes the IDENTICAL marker (it cannot recompute the per-process
    invocation nonce across process boundaries), so this run marker is the shared
    cross-process ownership signal both sides agree on. Falls back to a stable
    literal when ``RUN_ID`` is unset (ad-hoc invocation), mirroring
    ``unique_suffix``.
    """
    sid = os.environ.get("RUN_ID") or os.environ.get("LS_RUN_ID") or "adhoc"
    return f"isv-control-plane-owner:{sid}"


def _invocation_marker() -> str:
    """Return the invocation-specific ownership marker (fresh per create process).

    Unlike the run marker (shared by every same-run invocation) and the
    RUN_ID-derived short name, this marker embeds a nonce generated once per
    create_tenant process, so it is unique to THIS invocation. The create-side
    readbacks match on it to resolve ONLY the tags this exact invocation created:
    a same-run foreign resource that squats our short name and carries the run
    marker still lacks this nonce, so it is never adopted, resolved, or cleaned up
    as ours.
    """
    return f"isv-control-plane-invocation:{_INVOCATION_ID}"


def _key_description() -> str:
    """TagKey description: human label plus the run + invocation ownership markers."""
    return f"{_KEY_DESCRIPTION} [{_run_marker()}] [{_invocation_marker()}]"


def _value_description() -> str:
    """TagValue description: human label plus the run + invocation ownership markers."""
    return f"{_VALUE_DESCRIPTION} [{_run_marker()}] [{_invocation_marker()}]"


def _find_owned_tag_key(
    client: resourcemanager_v3.TagKeysClient, project: str, short_name: str
) -> resourcemanager_v3.TagKey | None:
    """Return the project TagKey with ``short_name`` AND this invocation's marker, or None.

    Matching requires BOTH the exact run-scoped short name and the
    INVOCATION-specific marker this exact create process stamps into every tag it
    creates -- not merely the shared run marker -- so a same-run foreign TagKey
    that squats our short name (and may even carry the run marker) is never
    resolved and can never be adopted or cleaned up by the create-failure paths.
    The full pager is materialized inside ``retry_idempotent_list`` so a transient
    on ANY page fetch -- not just the first request -- is retried, then matched.
    """
    marker = _invocation_marker()
    for key in retry_idempotent_list(client.list_tag_keys, parent=f"projects/{project}", op_desc="list_tag_keys"):
        if key.short_name == short_name and marker in (key.description or ""):
            return key
    return None


def _find_owned_tag_value(
    client: resourcemanager_v3.TagValuesClient, parent: str, short_name: str
) -> resourcemanager_v3.TagValue | None:
    """Return the TagValue under ``parent`` with ``short_name`` AND this invocation's marker, or None.

    Same invocation-scoped, exact-short-name match as ``_find_owned_tag_key`` so a
    same-run foreign value that squats our value short name under our key -- even
    one carrying the shared run marker -- is never adopted or cleaned up: it lacks
    this exact invocation's nonce. The full pager is materialized inside
    ``retry_idempotent_list`` so a transient on ANY page fetch is retried before
    the marker match.
    """
    marker = _invocation_marker()
    for value in retry_idempotent_list(client.list_tag_values, parent=parent, op_desc="list_tag_values"):
        if value.short_name == short_name and marker in (value.description or ""):
            return value
    return None


def _create_may_have_committed(error: Exception) -> bool:
    """Return whether a failed create RPC/wait may still have committed server-side.

    Separates a DEFINITE rejection from an AMBIGUOUS outcome so cleanup never
    drops a possibly-committed resource, and never invents cleanup for one
    provably not created:

      * A genuine 409 ``Conflict`` (AlreadyExists / Aborted) proves the short name
        was taken by another actor -- this run committed nothing, and the
        colliding resource is foreign (never adopted).
      * A definite pre-commit client rejection (permission-denied 403, not-found
        404) is applied before any mutation, so nothing was created.
      * Everything else -- a raw transport disconnect, or a 5xx / 429 / timeout
        transient (and any uncategorized call error) -- is AMBIGUOUS: the backend
        may have applied the create before the response was lost, so absence must
        be PROVEN by a marker-scoped readback and never assumed.
    """
    if isinstance(error, gax.Conflict):
        return False
    if is_transport_disconnect(error):
        return True
    return classify_gcp_error(error)[0] in {"transient", "api_error", "unknown_error"}


def _cleanup_partial(
    tv_client: resourcemanager_v3.TagValuesClient,
    tk_client: resourcemanager_v3.TagKeysClient,
    value_name: str | None,
    key_name: str | None,
) -> bool:
    """Retry-aware inline cleanup of a partial TagValue then its parent TagKey.

    Deletes children before the parent (a TagKey cannot be deleted while it owns
    values), each under the bounded ``delete_with_retry`` envelope, which retries
    transient / dependency-in-use failures and treats NotFound as the desired
    terminal state. Every cleanup error is still swallowed so it never masks the
    original create failure, but the outcome is CONFIRMED rather than discarded:
    returns True only when every requested delete is observably gone, and False
    when any delete could not be confirmed -- so the caller keeps a durable
    ownership handle for teardown instead of dropping a resource that may remain.
    The parent delete is skipped when the child was requested but not confirmed
    absent, so a stuck child never forces a doomed parent delete.
    """
    confirmed = True
    if value_name:
        if not delete_with_retry(
            lambda: tv_client.delete_tag_value(name=value_name).result(timeout=_TAG_OP_TIMEOUT),
            resource_desc=f"TagValue {value_name}",
        ):
            confirmed = False
            print(f"cleanup: delete TagValue {value_name} not confirmed", file=sys.stderr)
    if key_name:
        if value_name and not confirmed:
            # The child TagValue is not confirmed gone; deleting the parent TagKey
            # would fail while it still owns a value, so defer both to teardown.
            print(f"cleanup: skipped TagKey {key_name} delete; child not confirmed absent", file=sys.stderr)
        elif not delete_with_retry(
            lambda: tk_client.delete_tag_key(name=key_name).result(timeout=_TAG_OP_TIMEOUT),
            resource_desc=f"TagKey {key_name}",
        ):
            confirmed = False
            print(f"cleanup: delete TagKey {key_name} not confirmed", file=sys.stderr)
    return confirmed


def _recoverable_handle(
    key: resourcemanager_v3.TagKey | None,
    value: resourcemanager_v3.TagValue | None,
    value_short: str,
) -> str:
    """Return the exact delete_tenant ``--group-name`` for an unconfirmed cleanup.

    delete_tenant resolves the TagValue namespaced name and derives its parent
    TagKey by dropping the final segment, so a value-shaped handle lets teardown
    retry BOTH resources (NotFound on either is idempotent success). Prefer the
    resolved TagValue namespaced name; when only the parent TagKey was resolved,
    append the deterministic value short name so teardown's derived parent still
    resolves the leaked key. Returns "" when no resource was resolved, so the
    caller falls back to the empty sentinel (nothing exact to hand off).
    """
    if value is not None and value.namespaced_name:
        return value.namespaced_name
    if key is not None and key.namespaced_name:
        return f"{key.namespaced_name}/{value_short}"
    return ""


def _parent_only_handle(key: resourcemanager_v3.TagKey, project: str, key_short: str) -> str:
    """Return a PARENT-ONLY teardown handle for an owned TagKey with a foreign child value.

    Used whenever this invocation owns the parent TagKey but owns no child
    TagValue and the inline parent-only cleanup could not confirm the TagKey was
    deleted -- e.g. the child create collided (AlreadyExists) with a value this
    run does not own, failed pre-acceptance (never returned an operation), or was
    never submitted at all (a parent wait timed out before the child block).
    teardown needs the exact parent coordinates to retry deleting our TagKey, but
    must not delete any foreign value that may share our value short name. Emitting
    the parent's namespaced name with the reserved ``_PARENT_ONLY_VALUE`` segment
    does exactly that: delete_tenant
    resolves the parent by short name, finds no value matching the reserved
    segment, treats the foreign value as an unexpected sibling (retaining the
    parent and reporting failure rather than deleting foreign state), and deletes
    the parent only once no child remains. Falls back to the deterministic
    ``<project>/<key-short>`` when ``namespaced_name`` is unexpectedly empty so the
    handle is always parent-scoped and never carries the run-owned value short name.
    """
    base = key.namespaced_name if key.namespaced_name else f"{project}/{key_short}"
    return f"{base}/{_PARENT_ONLY_VALUE}"


def _try_cancel(operation: Any) -> None:
    """Best-effort cancel an outstanding async create whose blocking wait timed out.

    ``.result(timeout=...)`` timing out does NOT cancel the operation: the installed
    api_core polling future raises on the polling timeout and leaves the accepted
    operation running server-side, so the backend can still materialize the resource
    after we return. Cancellation is a separate explicit RPC. Requesting it shrinks
    the window in which a timed-out create can surface a resource behind our back.
    It is best-effort and never terminal proof: any failure is swallowed so it never
    masks the original wait failure, and the caller still keeps a durable ownership
    handle so teardown retries the exact scope regardless of whether cancel took.
    """
    if operation is None:
        return
    cancel = getattr(operation, "cancel", None)
    if cancel is None:
        return
    try:
        cancel()
    except Exception as e:  # cancellation is best-effort; never mask the wait failure
        print(f"cleanup: cancel outstanding tag operation failed: {e}", file=sys.stderr)


def _await_tag_create(op: Any) -> Any:
    """Resolve a tag create's return value to the created TagKey / TagValue.

    ``TagKeysClient.create_tag_key`` / ``TagValuesClient.create_tag_value`` return a
    long-running ``operation.Operation`` whose blocking ``.result()`` yields the
    created resource, and that is the normal path (a real operation handle is awaited
    up to ``_TAG_OP_TIMEOUT``). The create can resolve in two OTHER shapes that must
    not be mis-read as a wait failure -- which would send the caller's ambiguous
    handler to read the just-created tag back by its invocation marker and DELETE the
    tenant this call created, wrongly reporting a clean create as a failure with an
    empty teardown handoff:

      * SYNCHRONOUS resolution surfaces the materialized TagKey / TagValue directly
        instead of an operation handle. A resource has no callable ``result``
        attribute, so it is returned as-is.
      * An operation handle whose ``result`` does NOT accept a ``timeout`` keyword.
        Passing an unaccepted kwarg raises ``TypeError`` at call BINDING, before the
        method body runs (so no wait happened and no result was produced) -- awaiting
        with the bound timeout would then be mis-read as a create failure. Re-await
        that same handle WITHOUT the timeout so the created resource is returned.

    In every shape the returned value is grounded in what the create actually
    produced. A genuine conflict / transport / 5xx failure still surfaces from the
    operation's ``.result()`` (with or without the timeout) and flows to the caller's
    definite-vs-ambiguous split unchanged.
    """
    result_attr = getattr(op, "result", None)
    if not callable(result_attr):
        return op
    try:
        return result_attr(timeout=_TAG_OP_TIMEOUT)
    except TypeError as e:
        # Only a rejected ``timeout`` kwarg (unbound before the body runs) falls back
        # to an un-timed await; a TypeError raised inside a working wait propagates.
        if "timeout" not in str(e):
            raise
        return result_attr()


def _resolve_partial(finder: Any, *args: Any) -> tuple[Any, bool]:
    """Best-effort resolve a partially-created resource by its deterministic short name.

    Returns ``(resource_or_None, readback_ok)``. ``readback_ok`` is True when the
    readback completed at least once -- whether or not it located the resource --
    and False only when every attempt itself raised. A create/wait can be accepted
    by the backend and still fail our blocking wait, leaving a server-created
    resource that is NOT YET VISIBLE to a list issued immediately after the error
    (DELAYED VISIBILITY); the deterministic short name plus this invocation's marker
    let us find it once the backend settles. Because that visibility is delayed, the
    marker-scoped readback is a BOUNDED POLL -- retried ``_READBACK_ATTEMPTS`` times
    with escalating ``_READBACK_BACKOFF`` backoff -- rather than a single read: a
    resource that committed but was not yet listed is resolved on a later attempt,
    so the verdict is decided from the API's eventual state, never from one
    premature miss. Each attempt already retries transient list failures internally
    (``retry_idempotent_list``); this poll adds retry-on-ABSENCE on top. Resolution
    stays best-effort: a readback that raises must never mask the original create
    failure it is cleaning up after, and an exhausted poll that never located the
    resource must never be mistaken for confirmed absence -- so the caller keeps the
    deterministic recoverable handle whenever the resource is unresolved.
    """
    readback_ok = False
    for attempt in range(1, _READBACK_ATTEMPTS + 1):
        try:
            found = finder(*args)
            readback_ok = True
        except Exception as e:  # readback is best-effort; never mask the create failure
            print(f"cleanup: partial-resource readback failed: {e}", file=sys.stderr)
            found = None
        if found is not None:
            return found, True
        if attempt < _READBACK_ATTEMPTS:
            time.sleep(_READBACK_BACKOFF * attempt)
    return None, readback_ok


def _resolve_failed_key(
    tv_client: resourcemanager_v3.TagValuesClient,
    tk_client: resourcemanager_v3.TagKeysClient,
    project: str,
    key_short: str,
    *,
    key_op: Any,
    error: Exception,
    recoverable_handle: str,
) -> tuple[str, str]:
    """Return ``(teardown_handoff, tenant_key_id)`` after a NON-conflict TagKey failure.

    A genuine 409 conflict is handled by the caller (nothing owned -> empty
    sentinel). Here the create RPC or its wait failed for another reason. The
    second tuple element is the KNOWN permanent TagKey id whenever a readback
    positively resolves the owned parent, and empty when the id is genuinely
    unknown, so the caller forwards ``none`` only for a truly unknown id:

      * A DEFINITE pre-commit rejection (permission / not-found) created nothing,
        so the empty sentinel is safe -- teardown has nothing exact to delete and
        no id is known.
      * An AMBIGUOUS transport-drop / 5xx / timeout may have committed the TagKey.
        Best-effort cancel any accepted operation, then read back by the exact
        run-scoped short name AND this invocation's marker (never a foreign
        squatter that merely shares the short name, even one carrying the run
        marker). Clear to the empty sentinel (and empty id) ONLY when an owned key
        is resolved and its delete is confirmed gone; hand teardown a PARENT-ONLY
        handle AND the resolved permanent TagKey id when the owned key is resolved
        but its delete is unconfirmed, so delete_tenant can prove ownership and
        delete the leaked parent; and keep the deterministic handle with an empty
        id when the read cannot prove absence (a create-then-read miss under
        delayed visibility, or a read that itself failed) so a possibly-committed
        key is retried, never dropped, but ownership is not claimed on an id we
        never confirmed.
    """
    if not _create_may_have_committed(error):
        return "", ""
    _try_cancel(key_op)
    partial_key, readback_ok = _resolve_partial(_find_owned_tag_key, tk_client, project, key_short)
    cleaned = _cleanup_partial(
        tv_client, tk_client, value_name=None, key_name=partial_key.name if partial_key else None
    )
    if partial_key is not None and readback_ok and cleaned:
        return "", ""
    if partial_key is not None:
        return _parent_only_handle(partial_key, project, key_short), partial_key.name
    return recoverable_handle, ""


def _resolve_failed_value(
    tv_client: resourcemanager_v3.TagValuesClient,
    tk_client: resourcemanager_v3.TagKeysClient,
    key: resourcemanager_v3.TagKey,
    project: str,
    key_short: str,
    value_short: str,
    *,
    value_op: Any,
    error: Exception,
    recoverable_handle: str,
) -> tuple[str, str]:
    """Return ``(teardown_handoff, tenant_id)`` after a NON-conflict TagValue failure.

    The parent TagKey was created by THIS invocation and is owned, so every path
    cleans/keeps only that owned parent (plus any owned child), never a foreign
    value that merely shares our value short name. The second tuple element is the
    KNOWN permanent TagValue id whenever a readback positively resolves the owned
    child, and empty when the value id is genuinely unknown; the caller keeps the
    already-stamped ``tenant_key_id`` (the parent is owned) and forwards ``none``
    for ``tenant_id`` only when it is truly unknown:

      * A DEFINITE pre-commit value rejection created no value (id unknown ->
        empty): best-effort delete the owned parent and clear to the empty sentinel
        only when that delete is confirmed, else hand teardown a PARENT-ONLY handle.
      * An AMBIGUOUS transport-drop / 5xx / timeout may have committed the value.
        Best-effort cancel any accepted operation, then read back by the exact
        parent scope, value short name AND this invocation's marker. If an owned
        value is resolved its permanent id is now KNOWN: delete child-before-parent
        and clear to empty (empty id) only when both deletes confirm, else keep the
        exact resolved handle AND that resolved TagValue id so delete_tenant can
        prove ownership of the leaked value. If the read cannot prove the value's
        absence (a delayed-visibility create-then-read miss, or a read that itself
        failed), keep the deterministic handle with an empty id so teardown retries
        the value AND its parent -- a possibly-committed value is never dropped, but
        ownership is not claimed on an id we never confirmed.
    """
    if not _create_may_have_committed(error):
        if _cleanup_partial(tv_client, tk_client, value_name=None, key_name=key.name):
            return "", ""
        return _parent_only_handle(key, project, key_short), ""
    _try_cancel(value_op)
    partial_value, _readback_ok = _resolve_partial(_find_owned_tag_value, tv_client, key.name, value_short)
    if partial_value is not None:
        if _cleanup_partial(tv_client, tk_client, value_name=partial_value.name, key_name=key.name):
            return "", ""
        return _recoverable_handle(key, partial_value, value_short) or recoverable_handle, partial_value.name
    return recoverable_handle, ""


@handle_gcp_errors
def main() -> int:
    """Create a run-owned TagKey + TagValue tenant and print a structured JSON result."""
    parser = argparse.ArgumentParser(description="Create a control-plane tenant (Resource Manager tags)")
    parser.add_argument("--region", default="", help="Accepted for contract parity; tags are global")
    parser.add_argument("--project", default="", help="GCP project id (falls back to ADC)")
    parser.add_argument("--name-prefix", default="isv-tenant", help="Base tag short-name prefix")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenant_name": "",
        "tenant_id": "",
        "tenant_key_id": "",
    }

    project = resolve_project(args.project or None)
    tk_client = resourcemanager_v3.TagKeysClient()
    tv_client = resourcemanager_v3.TagValuesClient()

    key_short = unique_suffix(args.name_prefix)
    value_short = unique_suffix(f"{args.name_prefix}-val")

    # Deterministic recoverable handle -- the TagValue namespaced-name shape
    # (<project>/<key-short>/<value-short>) delete_tenant resolves. It is computed
    # here but NOT stamped into tenant_name yet: it is stamped only after a create
    # SUBMISSION is accepted (see below), so a pre-acceptance submission failure --
    # which owns nothing -- never hands teardown a predicted name this run did not
    # create. A failed readback after acceptance must never collapse to the empty
    # sentinel teardown treats as confirmed absence.
    recoverable_handle = f"{project}/{key_short}/{value_short}"

    # --- Create the parent TagKey --------------------------------------------
    # Separate SUBMISSION (create_tag_key returns an operation) from the blocking
    # WAIT (operation.result()), and separate a DEFINITE rejection from an
    # AMBIGUOUS create outcome. A genuine 409 conflict means the short name was
    # taken by another actor -- this run committed nothing and never adopts the
    # colliding TagKey. A definite pre-commit rejection (permission / not-found)
    # created nothing either. But a raw transport drop or a 5xx / 429 / timeout is
    # ambiguous: the backend may have created the TagKey before the response was
    # lost, so absence is PROVEN by a marker-scoped readback (never assumed) and a
    # non-empty teardown handoff is preserved until it is.
    key: resourcemanager_v3.TagKey | None = None
    key_op: Any = None
    try:
        key_op = tk_client.create_tag_key(
            tag_key=resourcemanager_v3.TagKey(
                parent=f"projects/{project}",
                short_name=key_short,
                description=_key_description(),
            )
        )
    except (gax.AlreadyExists, gax.Aborted) as e:
        # Genuine 409 conflict: a same-short-name TagKey already exists (or a
        # concurrent mutation aborted our create). The submission was never
        # accepted, so this run created nothing. Adopt nothing, resolve nothing by
        # the predicted short name, and hand teardown the empty sentinel so
        # delete_tenant is never pointed at foreign state. Matches the sister
        # object-store stub's bucket-Conflict rejection.
        result["tenant_name"] = ""
        result["error"] = f"CreateTagKey failed (short name already in use): {classify_gcp_error(e)[1]}"
        print(json.dumps(result, indent=2))
        return 1
    except Exception as e:
        # Non-conflict submission failure. No operation was returned, but an
        # ambiguous transport/5xx may still have committed the TagKey, so resolve
        # the teardown handoff from a marker-scoped readback rather than assuming
        # absence; a definite pre-commit rejection resolves to the empty sentinel.
        # A readback that positively resolves the owned parent also yields its
        # KNOWN permanent id, forwarded as tenant_key_id so teardown can prove
        # ownership and delete the leaked parent.
        result["tenant_name"], result["tenant_key_id"] = _resolve_failed_key(
            tv_client,
            tk_client,
            project,
            key_short,
            key_op=None,
            error=e,
            recoverable_handle=recoverable_handle,
        )
        result["error"] = classify_gcp_error(e)[1]
        print(json.dumps(result, indent=2))
        return 1

    # Submission ACCEPTED: an operation exists, so the backend may materialize the
    # TagKey even if the blocking wait times out. Stamp the deterministic
    # recoverable handle NOW (post-acceptance, pre-wait) so a wait failure can hand
    # teardown the exact coordinates of the resource this invocation owns.
    result["tenant_name"] = recoverable_handle
    try:
        # Resolve the created TagKey from the create return. Awaits a real operation
        # handle (``.result()``); if the create already resolved to the TagKey
        # itself, that resource is used as-is instead of being mis-read as a wait
        # failure that would delete the just-created key (see ``_await_tag_create``).
        key = _await_tag_create(key_op)
    except (gax.AlreadyExists, gax.Aborted) as e:
        # The accepted operation resolved to a genuine 409 conflict: the backend
        # created nothing for this run. Adopt nothing, clear to the empty sentinel.
        result["tenant_name"] = ""
        result["error"] = f"CreateTagKey failed (short name already in use): {classify_gcp_error(e)[1]}"
        print(json.dumps(result, indent=2))
        return 1
    except Exception as e:
        # Wait failed for a non-conflict reason, but the create SUBMISSION was
        # accepted, so the backend may have committed the TagKey even though the
        # blocking wait did not observe the operation result cleanly (a lost /
        # timed-out wait, a transport drop, or an operation handle whose result
        # surfaced in an unexpected shape). Decide the verdict from the API, not
        # from the wait exception: read the key back by its exact run-scoped short
        # name AND this invocation's marker. A positively-resolved OWNED key means
        # the create genuinely committed (a foreign squatter lacks this invocation's
        # nonce and is never resolved), so recover it and continue to the child
        # create -- a clean create is never reported as a spurious wait failure.
        # Only when the readback cannot confirm an owned key is the wait failure
        # terminal: resolve the handoff from a best-effort cancel plus the same
        # marker-scoped readback, preserving a non-empty handle until absence is
        # confirmed.
        recovered_key, _readback_ok = _resolve_partial(_find_owned_tag_key, tk_client, project, key_short)
        if recovered_key is not None:
            key = recovered_key
        else:
            # The wait failed and the readback could not confirm the parent, so the
            # teardown handoff is resolved from a best-effort cancel plus the same
            # marker-scoped readback; when that readback DOES resolve the owned
            # parent its KNOWN permanent id is forwarded as tenant_key_id.
            result["tenant_name"], result["tenant_key_id"] = _resolve_failed_key(
                tv_client,
                tk_client,
                project,
                key_short,
                key_op=key_op,
                error=e,
                recoverable_handle=recoverable_handle,
            )
            result["error"] = classify_gcp_error(e)[1]
            print(json.dumps(result, indent=2))
            return 1
    if key is None:
        # Defensive Optional-narrowing guard: a successful create_tag_key().result()
        # always returns a TagKey (the conflict/wait-failure branches above both
        # return early), so this only surfaces an impossible empty handle -- it never
        # adopts a pre-existing key.
        result["error"] = classify_gcp_error(
            RuntimeError(f"TagKey {key_short} create returned no handle under project {project}")
        )[1]
        print(json.dumps(result, indent=2))
        return 1

    # The parent TagKey is confirmed created and owned by THIS invocation, so its
    # backend-assigned permanent id is KNOWN. Stamp it NOW so every subsequent
    # child-create failure path hands teardown the exact TagKey id it needs to
    # prove ownership and delete the parent -- a known id is never dropped to the
    # ``none`` sentinel. Paths that CONFIRM the parent was deleted inline clear it
    # back to empty (nothing left to hand off).
    result["tenant_key_id"] = key.name

    # --- Create the child TagValue -------------------------------------------
    # Same submit-then-wait and definite-vs-ambiguous split as the parent. The
    # parent TagKey was created by THIS invocation and IS owned, so every
    # child-failure path cleans/keeps only that owned parent (plus any owned
    # child) -- never a foreign value that merely shares our value short name. A
    # genuine 409 conflict means a value this run does not own already occupies our
    # short name; it is rejected, never adopted.
    value: resourcemanager_v3.TagValue | None = None
    value_op: Any = None
    try:
        value_op = tv_client.create_tag_value(
            tag_value=resourcemanager_v3.TagValue(
                parent=key.name,
                short_name=value_short,
                description=_value_description(),
            )
        )
    except (gax.AlreadyExists, gax.Aborted) as e:
        # Genuine 409 child conflict: a foreign value occupies our value short name
        # under our freshly-created key. Best-effort delete ONLY the owned parent
        # (value_name=None) -- never the foreign value, which blocks that delete so
        # it may not confirm. Clear to the empty sentinel only when the parent
        # delete is CONFIRMED; otherwise hand teardown a PARENT-ONLY handle whose
        # reserved value segment matches no real value, so teardown retries our
        # owned TagKey while protecting the foreign colliding value. On a CONFIRMED
        # parent delete nothing remains, so the already-stamped tenant_key_id is
        # cleared; otherwise it is retained so teardown can delete the leaked key.
        if _cleanup_partial(tv_client, tk_client, value_name=None, key_name=key.name):
            result["tenant_name"] = ""
            result["tenant_key_id"] = ""
        else:
            result["tenant_name"] = _parent_only_handle(key, project, key_short)
        result["error"] = f"CreateTagValue failed (short name already in use): {classify_gcp_error(e)[1]}"
        print(json.dumps(result, indent=2))
        return 1
    except Exception as e:
        # Non-conflict child submission failure. Only the parent TagKey is owned; an
        # ambiguous transport/5xx may still have committed the value, so resolve the
        # handoff from a marker-scoped readback (cleaning child-before-parent)
        # rather than assuming absence; a definite rejection cleans just the parent.
        # A resolved child yields its KNOWN permanent id (tenant_id); the parent's
        # tenant_key_id stays stamped unless the handoff cleared to the empty
        # sentinel (parent confirmed deleted -> nothing left to hand off).
        result["tenant_name"], result["tenant_id"] = _resolve_failed_value(
            tv_client,
            tk_client,
            key,
            project,
            key_short,
            value_short,
            value_op=None,
            error=e,
            recoverable_handle=recoverable_handle,
        )
        if not result["tenant_name"]:
            result["tenant_key_id"] = ""
        result["error"] = classify_gcp_error(e)[1]
        print(json.dumps(result, indent=2))
        return 1

    # Child submission ACCEPTED: an operation exists, so the wait-failure path below
    # may resolve and clean a server-created TagValue this run owns by short name.
    try:
        # Resolve the created TagValue from the create return (awaits a real
        # operation handle, or uses an already-resolved TagValue directly) so a
        # synchronous create is not mis-read as a wait failure that would delete the
        # just-created value (see ``_await_tag_create``).
        value = _await_tag_create(value_op)
    except (gax.AlreadyExists, gax.Aborted) as e:
        # The accepted operation resolved to a genuine 409 conflict: a foreign value
        # occupies our short name and the backend created nothing for this run.
        # Clean only the owned parent; clear to empty (and drop the stamped
        # tenant_key_id) only on a confirmed parent delete, else a PARENT-ONLY
        # handle that never targets the foreign value while retaining tenant_key_id
        # so teardown can delete the leaked parent.
        if _cleanup_partial(tv_client, tk_client, value_name=None, key_name=key.name):
            result["tenant_name"] = ""
            result["tenant_key_id"] = ""
        else:
            result["tenant_name"] = _parent_only_handle(key, project, key_short)
        result["error"] = f"CreateTagValue failed (short name already in use): {classify_gcp_error(e)[1]}"
        print(json.dumps(result, indent=2))
        return 1
    except Exception as e:
        # Same API-grounded recovery as the parent: the value SUBMISSION was
        # accepted, so a non-conflict wait failure may still have committed the
        # TagValue. Decide from the API: read it back by the exact parent scope,
        # value short name AND this invocation's marker. A positively-resolved
        # OWNED value means the tenant was fully created, so recover it and fall
        # through to the success emit grounded in what the API returned -- a clean
        # create is never reported as a spurious wait failure. Only when the
        # readback cannot confirm an owned value is the wait failure terminal:
        # resolve the handoff from a best-effort cancel plus the same marker-scoped
        # readback (cleaning child-before-parent), preserving a non-empty handle
        # until absence is confirmed and never deleting a foreign value that merely
        # shares our value short name.
        recovered_value, _readback_ok = _resolve_partial(_find_owned_tag_value, tv_client, key.name, value_short)
        if recovered_value is not None:
            value = recovered_value
        else:
            # The wait failed and the readback could not confirm the value. Resolve
            # the handoff (cleaning child-before-parent); a resolved child yields
            # its KNOWN tenant_id, and the parent's tenant_key_id stays stamped
            # unless the handoff cleared to the empty sentinel (parent confirmed
            # deleted -> nothing left to hand off).
            result["tenant_name"], result["tenant_id"] = _resolve_failed_value(
                tv_client,
                tk_client,
                key,
                project,
                key_short,
                value_short,
                value_op=value_op,
                error=e,
                recoverable_handle=recoverable_handle,
            )
            if not result["tenant_name"]:
                result["tenant_key_id"] = ""
            result["error"] = classify_gcp_error(e)[1]
            print(json.dumps(result, indent=2))
            return 1
    if value is None:
        # Defensive Optional-narrowing guard: a successful create_tag_value().result()
        # always returns a TagValue (the conflict/wait-failure branches above both
        # return early), so this only surfaces an impossible empty handle. The parent
        # TagKey WAS created by this invocation, so clean up the key we own; clear the
        # handle (and the stamped tenant_key_id) only when that delete is confirmed,
        # otherwise hand the exact owned coordinates AND the known tenant_key_id to
        # teardown so the parent is not orphaned. No value was ever confirmed, so
        # tenant_id stays empty (genuinely unknown).
        if _cleanup_partial(tv_client, tk_client, value_name=None, key_name=key.name):
            result["tenant_name"] = ""
            result["tenant_key_id"] = ""
        else:
            result["tenant_name"] = _recoverable_handle(key, None, value_short) or recoverable_handle
        result["error"] = classify_gcp_error(
            RuntimeError(f"TagValue {value_short} create returned no handle under {key.name}")
        )[1]
        print(json.dumps(result, indent=2))
        return 1

    # Both creates completed. Confirm the value exists by an exact, marker-scoped
    # readback so the emitted lifecycle handoff is grounded in what the API returns
    # -- not just the create-operation echo -- then emit the authoritative
    # namespaced name. Fall back to the deterministic handle so the forwarded
    # handoff is never empty on a clean create even if the readback (or the echoed
    # namespaced name) is unavailable.
    final_value: resourcemanager_v3.TagValue = value
    confirmed, _confirmed_ok = _resolve_partial(_find_owned_tag_value, tv_client, key.name, value_short)
    if confirmed is not None:
        final_value = confirmed
    result["tenant_name"] = final_value.namespaced_name or recoverable_handle
    result["tenant_id"] = final_value.name
    # Forward the two backend-assigned PERMANENT ids so delete_tenant can prove
    # ownership by exact id match rather than by the recomputable run marker alone
    # (a same-run foreign resource can carry that marker AND squat our short name).
    # These are the unforgeable identity of the exact tenant THIS run created.
    result["tenant_key_id"] = key.name
    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
