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

"""GCP error classification + retry helpers.

Emits the provider-neutral structured error shape:
    {"success": false, "error_type": "<bucket>", "error": "[bucket=<name>] <msg>"}

``error_type`` maps each google.api_core exception (and google.auth ADC error)
to a shared disposition bucket so callers branch on the bucket, never the raw
exception class:
    credentials_missing, credentials_invalid, access_denied, not_found,
    conflict, transient, api_error (uncategorized call error), unknown_error.
The ``transient`` bucket is the retryable disposition — the retry helpers below
(delete_with_retry / modify_iam_policy_with_retry) apply bounded backoff to it
where the operation is safe to retry. Every emitted error string carries a
leading ``[bucket=<name>]`` token so downstream diagnostics and retry decisions
read the disposition directly instead of re-parsing the wire message.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from google.api_core import exceptions as gax
from google.auth import exceptions as auth_exceptions

logger = logging.getLogger(__name__)

ALREADY_GONE_EXCEPTIONS: tuple[type[Exception], ...] = (gax.NotFound,)

TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    gax.ServiceUnavailable,
    gax.InternalServerError,
    gax.GatewayTimeout,
    gax.DeadlineExceeded,
    gax.TooManyRequests,
    gax.Aborted,
    gax.RetryError,
)

_TRANSIENT_CANDIDATES: tuple[type[Exception], ...] = (
    *TRANSIENT_EXCEPTIONS,
    auth_exceptions.RefreshError,
)


def _is_transient_exception(e: Exception) -> bool:
    """Return whether ``e`` is safe to retry under the transient budget."""
    return isinstance(e, TRANSIENT_EXCEPTIONS) or (isinstance(e, auth_exceptions.RefreshError) and e.retryable)


# Compute Engine reports the dependency-in-use condition (a resource cannot
# be deleted until its dependents finish draining) as an HTTP 400 ->
# gax.BadRequest (or, on an async DONE-with-errors op surfaced by the
# op-wait helpers, a RuntimeError carrying the same wire code). This is the
# Compute Engine analog of the AWS provider's DependencyViolation, which the
# AWS provider's teardown retries with backoff (see
# providers/aws/scripts/network/teardown.py delete_with_retry). It is
# eventually-consistent — a delete issued right after a dependent's delete
# op reports DONE can still race the backend dependency-graph drain — so it
# MUST be retried, not treated as terminal.
# Pre-acceptance "parent resource not ready" markers. Compute Engine can reject a
# dependent-resource create (e.g. subnetworks.insert issued right after the parent
# network's insert op reported DONE) with HTTP 400 ``resourceNotReady`` / "resource
# is not ready" during the brief eventual-consistency window before the parent is
# usable as a create target. This is the create-path sibling of the dependency-in-
# use delete race below; both are eventually-consistent HTTP 400s that must be
# retried rather than failed.
_RESOURCE_NOT_READY_MARKERS: tuple[str, ...] = (
    "resourcenotready",
    "resource is not ready",
)

_DEPENDENCY_IN_USE_MARKERS: tuple[str, ...] = (
    "resourceinusebyanotherresource",
    "resource is being used",
    "is already being used",
    "is being used by",
    *_RESOURCE_NOT_READY_MARKERS,
)


def _is_dependency_in_use(e: Exception) -> bool:
    """Return True iff ``e`` signals the dependency-in-use (retryable) condition."""
    msg = str(e).lower()
    return any(marker in msg for marker in _DEPENDENCY_IN_USE_MARKERS)


def is_resource_not_ready(e: Exception) -> bool:
    """Return True iff ``e`` is the pre-acceptance 'parent resource not ready' 400.

    NARROW create-path classifier: matches ONLY the ``resourceNotReady`` /
    "resource is not ready" HTTP 400 that Compute Engine returns when a dependent
    create (e.g. ``subnetworks.insert``) races the eventual-consistency window
    after its parent network's insert op reached DONE. Callers gate a create retry
    on THIS predicate only, so the retry never widens to conflicts, quota,
    permission, or ambiguous 5xx create responses — those still surface
    immediately. This is the GCP analog of the AWS oracle waiting for
    ``vpc_available`` before creating its subnet.
    """
    msg = str(e).lower()
    return any(marker in msg for marker in _RESOURCE_NOT_READY_MARKERS)


def classify_gcp_error(e: Exception) -> tuple[str, str]:
    """Translate a GCP exception into ``(bucket, "[bucket=<name>] message")``.

    Buckets follow the shared google.api_core disposition taxonomy: callers
    branch on the returned bucket, never the raw exception class. The returned
    message is prefixed with a ``[bucket=<name>]`` token so the disposition
    survives into every emitted ``error`` string.

    Helpers that add context by wrapping a typed google exception in a plain
    ``RuntimeError(...) from exc`` (e.g. ``resolve_project`` chaining a
    ``DefaultCredentialsError``, or ``region_zones`` chaining a ``NotFound`` /
    ``PermissionDenied``) would otherwise collapse to ``unknown_error`` and lose
    the actionable bucket. When the top exception is unclassified we therefore
    walk the ``__cause__`` / ``__context__`` chain and adopt the first typed
    bucket found, while keeping the OUTER contextual message intact.
    """
    bucket, detail = _bucket_and_detail(e)
    if bucket == "unknown_error":
        chained = _chained_bucket(e)
        if chained is not None:
            bucket = chained
    return bucket, f"[bucket={bucket}] {detail}"


def _chained_bucket(e: Exception, *, max_depth: int = 10) -> str | None:
    """Return the first classifiable bucket in ``e``'s cause/context chain.

    Traverses ``__cause__`` (explicit ``raise ... from``) then ``__context__``
    (implicit chaining) up to ``max_depth`` links, returning the disposition
    bucket of the first typed google exception found. Returns ``None`` when the
    whole chain classifies as ``unknown_error`` so the caller keeps that bucket.
    Cycle-guarded so a self-referential ``__context__`` cannot loop.
    """
    seen: set[int] = set()
    current: BaseException | None = e
    for _ in range(max_depth):
        nxt = current.__cause__ or current.__context__
        if nxt is None or id(nxt) in seen:
            return None
        seen.add(id(nxt))
        if isinstance(nxt, Exception):
            bucket, _detail = _bucket_and_detail(nxt)
            if bucket != "unknown_error":
                return bucket
        current = nxt
    return None


def _bucket_and_detail(e: Exception) -> tuple[str, str]:
    """Map ``e`` to its disposition bucket and human detail (no token prefix).

    Order: credentials_missing (no ADC at all) before credentials_invalid
    (ADC present but rejected) so a missing-setup operator gets the
    setup-pointing message rather than the "invalid credentials" one. The 409
    ``conflict`` check precedes the ``transient`` tuple because ``gax.Aborted``
    subclasses ``gax.Conflict`` — a name/state collision is surfaced, not
    retried blindly.
    """
    if isinstance(e, auth_exceptions.DefaultCredentialsError):
        return "credentials_missing", f"GCP credentials missing or not configured: {e}"
    if isinstance(e, auth_exceptions.RefreshError):
        if e.retryable:
            return "transient", f"GCP credential refresh transiently failed: {e}"
        return "credentials_invalid", f"GCP credentials invalid or expired: {e}"
    if isinstance(e, gax.Unauthenticated):  # HTTP 401 / gRPC 16
        return "credentials_invalid", f"GCP credentials invalid or expired: {e}"
    if isinstance(e, gax.PermissionDenied):  # HTTP 403 / gRPC 7
        return "access_denied", f"Access denied: {e}"
    if isinstance(e, gax.NotFound):  # HTTP 404 / gRPC 5
        return "not_found", str(e)
    if isinstance(e, gax.Conflict):  # HTTP 409: AlreadyExists / Aborted subclass too
        return "conflict", str(e)
    if isinstance(
        e,
        (
            gax.ResourceExhausted,  # HTTP 429
            gax.ServiceUnavailable,  # HTTP 503
            gax.InternalServerError,  # HTTP 500
            gax.DeadlineExceeded,  # HTTP 504
            gax.GatewayTimeout,  # HTTP 504
            gax.TooManyRequests,  # HTTP 429
            gax.RetryError,  # retries exhausted on an underlying transient
        ),
    ):
        return "transient", str(e)
    if isinstance(e, gax.GoogleAPICallError):
        return "api_error", str(e)
    return "unknown_error", str(e)


# Raw transport disconnects (NOT google.api_core exceptions)
# ---------------------------------------------------------------------
# The Compute Engine HTTP transport can drop the TCP connection
# mid-call, surfacing a low-level
# ``http.client.RemoteDisconnected`` / urllib3 ``ProtocolError`` — either
# unwrapped, or re-wrapped in a ``requests`` ``ConnectionError``. These are NOT
# ``google.api_core`` types, so ``TRANSIENT_EXCEPTIONS`` and the disposition
# classifier never see them; an idempotent read/list/delete/poll would then fail
# on the first drop. This helper applies a single 2-second-delayed retry on
# IDEMPOTENT operations only (describe/list/delete/poll); NON-idempotent creates
# must NOT be retried here because a re-issued create can double-provision.
_TRANSPORT_DISCONNECT_CLASS_NAMES: frozenset[str] = frozenset({"RemoteDisconnected", "ProtocolError"})
_TRANSPORT_DISCONNECT_MARKERS: tuple[str, ...] = (
    "remote end closed connection",
    "connection aborted",
    "connection reset",
    "connection broken",
)


def is_transport_disconnect(e: BaseException, *, max_depth: int = 10) -> bool:
    """Return True iff ``e`` (or a wrapped cause) is a raw transport disconnect.

    NARROW low-level-connection-drop classifier. Matches the
    ``http.client.RemoteDisconnected`` / urllib3 ``ProtocolError`` family the
    Compute transport raises when the backend closes the socket mid-call,
    detected by class name plus a small set of disconnect message markers (so we
    do not hard-import urllib3 / requests). Walks the ``__cause__`` /
    ``__context__`` chain up to ``max_depth`` links because ``requests``
    re-wraps the underlying urllib3 error, and is cycle-guarded. Every
    ``google.api_core`` error (name/state 4xx, quota, 5xx) is a typed class that
    never matches here, so this cannot widen the retry onto real API failures.
    """
    seen: set[int] = set()
    current: BaseException | None = e
    for _ in range(max_depth):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        if type(current).__name__ in _TRANSPORT_DISCONNECT_CLASS_NAMES:
            return True
        if any(marker in str(current).lower() for marker in _TRANSPORT_DISCONNECT_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def retry_idempotent(
    fn: Callable[..., Any],
    *args: Any,
    op_desc: str = "operation",
    retries: int = 1,
    transient_retries: int = 3,
    backoff_seconds: float = 2.0,
    **kwargs: Any,
) -> Any:
    """Call an IDEMPOTENT get/list/delete/poll ``fn``, retrying transient failures.

    Wraps a single provider read/list/delete/poll call and retries TWO
    independent retryable classes:

      * The typed transient exceptions (``TRANSIENT_EXCEPTIONS`` — the
        429 / 5xx / timeout classes such as
        ``ResourceExhausted``, ``ServiceUnavailable``, ``InternalServerError``,
        and ``DeadlineExceeded``). Retried ``transient_retries`` times (default
        3) with escalating ``backoff_seconds * attempt`` backoff, matching
        ``delete_with_retry``. A single retryable 429/5xx during a read, list,
        poll, or delete therefore no longer aborts the run despite an idempotent
        request — the installed Compute / Cloud Logging / Resource Manager
        clients supply no default retry for their reads.
      * A raw transport disconnect (``is_transport_disconnect`` —
        ``RemoteDisconnected`` / urllib3 ``ProtocolError``, which is NOT a
        ``google.api_core`` type and so never lands in ``TRANSIENT_EXCEPTIONS``).
        Retried ``retries`` times (default once) after a flat ``backoff_seconds``
        delay — the pre-existing raw-drop contract, preserved unchanged.

    Every OTHER exception — name/state 4xx (``NotFound``, ``Conflict``,
    ``PermissionDenied``, ``Unauthenticated``), ``InvalidArgument``, and any
    uncategorized ``GoogleAPICallError`` — propagates unchanged to the caller's
    existing classifiers. MUST wrap only idempotent operations: a retried create
    could double-provision (a 5xx can arrive after the server already created the
    resource), so non-idempotent create paths never call this helper.
    """
    transport_attempt = 0
    transient_attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except _TRANSIENT_CANDIDATES as e:
            if not _is_transient_exception(e) or transient_attempt >= transient_retries:
                raise
            transient_attempt += 1
            delay = backoff_seconds * transient_attempt
            logger.warning(
                "Transient error during idempotent %s (retry %d/%d): %s; retrying in %.1fs",
                op_desc,
                transient_attempt,
                transient_retries,
                e,
                delay,
            )
            time.sleep(delay)
        except Exception as e:
            if transport_attempt >= retries or not is_transport_disconnect(e):
                raise
            transport_attempt += 1
            logger.warning(
                "Transport disconnect during idempotent %s (retry %d/%d): %s; retrying in %.1fs",
                op_desc,
                transport_attempt,
                retries,
                e,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)


def retry_idempotent_list(list_fn: Callable[..., Any], *, op_desc: str = "list", **kwargs: Any) -> list[Any]:
    """Materialize a FULL paginated listing under the idempotent-retry envelope.

    The installed Compute / Resource Manager ``list_*`` RPCs return a LAZY pager:
    only the initial request is issued eagerly, and iterating the pager fetches
    each later page on demand. If ``retry_idempotent(list_fn, ...)`` is called and
    its returned pager is iterated afterward, every later-page request is issued
    OUTSIDE the retry envelope -- so only the first page gets the full
    set of transient errors (429 / ServiceUnavailable / InternalServerError /
    DeadlineExceeded / transport disconnect) retried, and the deferred page
    fetches fall back to the SDK's partial default policy (ServiceUnavailable
    only). Forcing the complete ``list(...)`` materialization INSIDE the callable
    passed to ``retry_idempotent`` retries the full taxonomy on EVERY page fetch
    (a transient re-lists from the first page, which is safe for idempotent
    reads), then returns the materialized list so callers match over a plain list
    rather than a lazy pager. Mirrors the sister helper in
    ``common/service_account._list_service_account_emails``.
    """
    return retry_idempotent(lambda: list(list_fn(**kwargs)), op_desc=op_desc)


def delete_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    resource_desc: str = "resource",
    attempts: int = 5,
    backoff_seconds: float = 2.0,
    **kwargs: Any,
) -> bool:
    """Call ``fn`` with bounded retry on transient + dependency-in-use errors.

    Never raises. Returns True iff the call succeeded or the resource was
    already gone (NotFound counts as success — the desired terminal state
    is reached). Mirrors providers/aws/scripts/common/errors.delete_with_retry
    so callers can write provider-portable cleanup blocks.

    Two retryable classes (otherwise terminal -> return False):

      * Rate-limit / 5xx / timeout (``TRANSIENT_EXCEPTIONS``).
      * Dependency-in-use (``_is_dependency_in_use``) — the eventually-
        consistent condition where a resource cannot yet be deleted because
        a dependent is still draining after its own delete op reported DONE.
        Surfaces as ``gax.BadRequest`` or its ``gax.FailedPrecondition``
        subclass — BOTH HTTP 400 in the installed SDK
        (``FailedPrecondition(BadRequest)``, code 400); HTTP 412 is the
        DISTINCT ``gax.PreconditionFailed`` class, which is intentionally
        NOT handled here — or, on an async DONE-with-errors op re-raised by
        the op-wait helpers, as a ``RuntimeError`` carrying the same wire code.
        This mirrors the AWS provider retrying ``DependencyViolation``; the
        in-use backoff escalates faster (the drain takes longer than a rate
        limit). Non-in-use 400s stay terminal so simple bad-request deletes
        do not spin.

    The bool return MUST be consumed by the caller and AND-ed into the
    teardown result — helpers that return ``bool`` for batch-cleanup
    safety MUST surface the bool into ``result['success']``.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            fn(*args, **kwargs)
            return True
        except ALREADY_GONE_EXCEPTIONS:
            return True
        except _TRANSIENT_CANDIDATES as e:
            if not _is_transient_exception(e):
                logger.exception("Non-retryable credential error deleting %s", resource_desc)
                return False
            if attempt < attempts:
                last_error = e
                delay = backoff_seconds * attempt
                logger.warning(
                    "Transient error deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc,
                    attempt,
                    attempts,
                    e,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Failed to delete %s after %d attempts", resource_desc, attempts)
            return False
        except (gax.BadRequest, gax.FailedPrecondition) as e:
            # Dependency-in-use (an HTTP 400 — gax.FailedPrecondition subclasses
            # gax.BadRequest and is also 400) is retryable; any other bad-request
            # is a terminal misuse that retrying cannot fix. HTTP 412 is the
            # distinct gax.PreconditionFailed class and is intentionally not
            # caught here: no transient HTTP 412 delete response is expected for
            # these resources, so a 412 stays terminal.
            if _is_dependency_in_use(e) and attempt < attempts:
                last_error = e
                delay = backoff_seconds * (attempt + 1) * 2
                logger.warning(
                    "Dependency-in-use deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc,
                    attempt,
                    attempts,
                    e,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Non-retryable bad-request deleting %s", resource_desc)
            return False
        except gax.GoogleAPICallError:
            logger.exception("Non-transient API error deleting %s", resource_desc)
            return False
        except Exception as e:
            # A raw transport disconnect (RemoteDisconnected / urllib3
            # ProtocolError, possibly re-wrapped) during this idempotent delete
            # is retryable — re-issuing the delete is safe (NotFound on the
            # retry counts as success), so retry after the flat 2s
            # ``backoff_seconds`` delay.
            if is_transport_disconnect(e) and attempt < attempts:
                last_error = e
                logger.warning(
                    "Transport disconnect deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc,
                    attempt,
                    attempts,
                    e,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                continue
            # The op-wait helpers raise RuntimeError carrying the op error
            # code; a dependency-in-use code surfaces here and is retryable.
            if _is_dependency_in_use(e) and attempt < attempts:
                last_error = e
                delay = backoff_seconds * (attempt + 1) * 2
                logger.warning(
                    "Dependency-in-use (op error) deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc,
                    attempt,
                    attempts,
                    e,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Unexpected error deleting %s", resource_desc)
            return False

    if last_error is not None:
        logger.error("Exhausted retries deleting %s: %s", resource_desc, last_error)
    return False


def modify_iam_policy_with_retry(
    read_policy: Callable[[], Any],
    write_policy: Callable[[Any], Any],
    mutate: Callable[[Any], None],
    *,
    resource_desc: str = "resource",
    attempts: int = 5,
    backoff_seconds: float = 2.0,
) -> None:
    """Read-modify-write an IAM policy with bounded retry (refresh-GET each attempt).

    A GET-then-SET IAM-policy mutation races two transient conditions: a
    backend 5xx / 429, and a stale-etag conflict (HTTP 409 -> ``gax.Aborted``)
    when another writer updated the policy between the read and the write. Both
    are retryable, and on retry the policy MUST be re-read so the fresh etag is
    used — so ``read_policy`` is called anew each attempt, ``mutate`` applies
    the change in place, and ``write_policy`` commits it.

    This is the IAM-policy-mutation analog of ``delete_with_retry`` (which only
    covers the delete surface): a single ``get_iam_policy`` -> append binding ->
    ``set_iam_policy`` without this envelope aborts the whole caller on the
    first transient. Both classify under ``TRANSIENT_EXCEPTIONS`` (which already
    includes ``gax.Aborted``). Re-raises the last error once the budget is
    exhausted so the caller folds the failure into a structured error rather
    than silently dropping the binding.
    """
    for attempt in range(1, attempts + 1):
        try:
            policy = read_policy()
            mutate(policy)
            write_policy(policy)
            return
        except _TRANSIENT_CANDIDATES as e:
            if not _is_transient_exception(e):
                raise
            if attempt >= attempts:
                logger.exception("Failed to modify IAM policy on %s after %d attempts", resource_desc, attempts)
                raise
            delay = backoff_seconds * attempt
            logger.warning(
                "Transient error modifying IAM policy on %s (attempt %d/%d): %s; retrying in %.1fs",
                resource_desc,
                attempt,
                attempts,
                e,
                delay,
            )
            time.sleep(delay)


def handle_gcp_errors[**P](func: Callable[P, int]) -> Callable[P, int]:
    """Decorator that catches uncaught GCP errors and emits structured JSON.

    Mirrors providers/aws/scripts/common/errors.handle_aws_errors. Scripts
    still print their own JSON and return 0/1; this decorator only handles
    exceptions that escape main (e.g. client construction).
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_type, error_msg = classify_gcp_error(e)
            print(
                json.dumps(
                    {"success": False, "error_type": error_type, "error": error_msg},
                    indent=2,
                )
            )
            return 1

    return wrapper
