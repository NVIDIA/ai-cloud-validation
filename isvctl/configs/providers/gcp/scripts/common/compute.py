# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Shared Compute Engine helpers for GCP VM stubs.

This module is the canonical home for Compute Engine divergences from
the AWS provider:

  * ``resolve_project`` — env / ADC project resolution because the
    harness does NOT forward GOOGLE_CLOUD_PROJECT to spawned stubs.
  * ``narrow_region_to_zone`` / ``zone_to_region`` — Compute Engine
    instance APIs are zone-scoped; provider configs may supply either.
  * ``canonical_state`` — translate Compute Engine raw status to the
    canonical lifecycle vocabulary the suite expects.
  * ``wait_for_zonal_op`` / ``wait_for_global_op`` — block on a
    Compute Operation's terminal DONE.
  * ``poll_instance_state`` — poll instances.get for canonical state.
  * ``wait_for_public_ip`` — ephemeral external IPs are released on stop;
    post-start / post-reset code MUST re-read rather than reuse a cached
    arg.
  * ``generate_ssh_keypair`` — local PEM/.pub pair; returns
    ``(path, created)`` for the verified-reuse cleanup contract.
  * ``ensure_ssh_firewall`` — verified-reuse SSH firewall rule on the
    target network; returns ``(name, created)``.
  * Label projection helpers — canonical mixed-case Name/CreatedBy tags
    project to api-valid lowercase labels on create and back on read.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import google.auth
from google.api_core import exceptions as gax
from google.cloud import compute_v1

# --------------------------------------------------------------------- #
# Auth / project resolution                                             #
# --------------------------------------------------------------------- #

# The harness does NOT forward GOOGLE_CLOUD_PROJECT/GCLOUD_PROJECT to
# spawned stubs. Fall back to
# Application Default Credentials' bundled project_id when neither --project
# nor an env var is set so operators with `gcloud auth application-default
# login` don't have to thread the project through every call.
_PROJECT_ENV_VARS: tuple[str, ...] = ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT")


def resolve_project(arg_value: str | None = None) -> str:
    """Resolve the active GCP project ID.

    Order: explicit ``--project`` arg, then ``GOOGLE_CLOUD_PROJECT`` /
    ``GCLOUD_PROJECT`` env var, then ``google.auth.default()`` (ADC).
    Raises ``RuntimeError`` if nothing resolves so the failure surfaces as
    a structured ``credentials_missing``-class error rather than a hidden
    AttributeError downstream.
    """
    if arg_value:
        return arg_value
    for var in _PROJECT_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    try:
        _, project = google.auth.default()
    except Exception as e:
        raise RuntimeError(
            f"Could not resolve GCP project ID via ADC: {e}. "
            "Run `gcloud auth application-default login` or pass --project."
        ) from e
    if not project:
        raise RuntimeError(
            "GCP project ID not found. Set GOOGLE_CLOUD_PROJECT, pass --project, "
            "or run `gcloud auth application-default login` with a project quota."
        )
    return project


# --------------------------------------------------------------------- #
# Run-id suffixing                                                      #
# --------------------------------------------------------------------- #


def unique_suffix(base: str, *, length: int = 8) -> str:
    """Append the suite's ``RUN_ID`` (or a fresh UUID8) to ``base``.

    Compute Engine resource names ARE the API IDs (name-collision
    risk). Every user-supplied name in this provider — instance,
    firewall, local
    key file — MUST flow through this helper so that:

      * Concurrent test runs (different ``RUN_ID``s) don't collide
        on ``AlreadyExists`` during create.
      * Operators can group artifacts by run id (``gcloud compute
        instances list --filter "name~$RUN_ID"``).
      * Same-session teardown deletes only its own resources.

    Falls back to a random UUID8 only when ``RUN_ID`` is unset (e.g.
    manual stub invocation without the harness setting the env var).
    The helper MUST NOT raise on missing env var — that would block
    ad-hoc reproduction.
    """
    sid = os.environ.get("RUN_ID") or os.environ.get("LS_RUN_ID") or ""
    return f"{base}-{sid[:length] if sid else uuid.uuid4().hex[:length]}"


# --------------------------------------------------------------------- #
# Zone / region helpers                                                 #
# --------------------------------------------------------------------- #


# Preferred GPU zones for L4 capacity walk. The list captures a regional
# priority order plus capacity-advised sibling zones observed in live
# Compute Engine stockout responses, so the multi-zone walker is not
# limited to stale static availability notes. Refresh this list when
# operators update priorities.
PREFERRED_ZONES: tuple[str, ...] = (
    "us-central1-a",
    "us-central1-b",
    "us-central1-c",
    "us-east4-a",
    "us-east4-b",
    "us-east4-c",
    # us-east1 has L4 capacity in c + d only; us-east1-b deliberately
    # omitted (no L4).
    "us-east1-c",
    "us-east1-d",
    "us-west1-a",
    "us-west1-b",
    "us-west4-a",
    "us-west4-b",
    "europe-west4-a",
    "europe-west4-b",
    "europe-west1-b",
    "europe-west1-c",
    "asia-southeast1-a",
    "asia-southeast1-b",
    "asia-southeast1-c",
    "asia-northeast1-a",
    "asia-northeast1-c",
    "asia-east1-a",
    "asia-east1-b",
    "asia-east1-c",
)


_ZONE_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+-[a-z]+$")


# Compute Engine GPU machine-type families that REJECT the default
# `Scheduling.on_host_maintenance=MIGRATE` and require TERMINATE +
# automatic_restart. This Scheduling override is GPU-only — non-GPU
# types must keep the API
# default to preserve live-migrate behavior. The prefix list covers the
# documented GPU families (g2 = L4, a2 = A100, a3 = H100/H200, n1 with
# attached guest accelerators); extend the prefix list here as new GPU
# families ship.
_GPU_MACHINE_PREFIXES: tuple[str, ...] = ("g2-", "a2-", "a3-")


def is_gpu_machine_type(machine_type: str) -> bool:
    """Return True iff the machine type is a known GPU-bearing family."""
    return any(machine_type.startswith(p) for p in _GPU_MACHINE_PREFIXES)


def _is_full_zone(value: str) -> bool:
    """Return True iff ``value`` is a full zone (region prefix + letter)."""
    return bool(_ZONE_RE.match(value))


_REGION_LOOKUP_RETRY_TRANSIENT: tuple[type[BaseException], ...] = (
    gax.ServiceUnavailable,
    gax.DeadlineExceeded,
    gax.Aborted,
    gax.ResourceExhausted,
    gax.InternalServerError,
)


def _list_region_zones(
    project: str,
    region: str,
    *,
    attempts: int = 3,
    backoff: float = 1.0,
) -> list[str]:
    """Return the live zones the GCP API reports for ``region``.

    The static ``PREFERRED_ZONES`` list omits regions the suite has
    not pinned for L4 capacity; the helper consults the API so a
    valid region without
    a preferred-list entry still walks its OWN zones first instead of
    silently leaving the region for the preferred-list head.

    For ``zone_capacity_handling`` the helper MUST distinguish:

    * Transient lookup errors (HTTP 5xx / 429 / quota) — retry with
      backoff, then return ``[]`` so the caller can fall back to the
      offline prefix-match path against ``PREFERRED_ZONES``.
    * Terminal errors (``NotFound`` / ``PermissionDenied`` /
      ``InvalidArgument`` / ``Unauthenticated``) — raise structured so
      the operator's region request is NEVER silently substituted by a
      different region's zones.
    """
    last_transient: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            client = compute_v1.RegionsClient()
            region_obj = client.get(project=project, region=region)
            return [url.rsplit("/", 1)[-1] for url in region_obj.zones or ()]
        except _REGION_LOOKUP_RETRY_TRANSIENT as e:
            last_transient = e
            print(
                f"  region zones lookup transient ({type(e).__name__}); "
                f"attempt {attempt}/{attempts}",
                file=sys.stderr,
            )
            time.sleep(backoff * attempt)
            continue
        except (
            gax.NotFound,
            gax.PermissionDenied,
            gax.InvalidArgument,
            gax.Unauthenticated,
        ) as e:
            msg = f"region '{region}' is invalid or unauthorized: {e}"
            raise RuntimeError(msg) from e
        except gax.GoogleAPICallError as e:
            # Other GCP API call errors are treated as terminal — never
            # silently substitute a different region's zones.
            msg = f"region '{region}' lookup failed: {e}"
            raise RuntimeError(msg) from e
    if last_transient is not None:
        print(
            f"  region zones lookup exhausted after {attempts} attempts: "
            f"{last_transient}",
            file=sys.stderr,
        )
    return []


def select_zones(
    region_or_zone: str | None,
    *,
    project: str | None = None,
    zone_walk: bool = True,
) -> list[str]:
    """Return the ordered list of candidate zones for an ``instances.insert`` walk.

    Zone-capacity handling:

      * A FULL zone string (e.g., ``us-central1-f``) is a single-zone
        pin: honor it as the ONLY candidate, no walk fallback.
      * A region prefix (e.g., ``us-central1``) is expanded via the GCP
        RegionsClient. Intersect with ``PREFERRED_ZONES``: if the
        intersection is non-empty, walk those zones in preferred-list
        order, then fall back cross-region to the rest of
        ``PREFERRED_ZONES``. ONLY when the region has no preferred
        zone live (e.g., region missing from the curated capacity list)
        do we fall back to the region's other live zones — preserves the
        operator-configured capacity ordering while keeping an
        offline-region escape hatch.
      * An empty value falls back to the full ``PREFERRED_ZONES``.

    Setting ``zone_walk=False`` returns at most one candidate so
    callers can disable the walk locally (mirrors the AWS provider's
    no-fallback behavior for unit tests).
    """
    if not region_or_zone:
        return list(PREFERRED_ZONES) if zone_walk else list(PREFERRED_ZONES[:1])

    if _is_full_zone(region_or_zone):
        return [region_or_zone]

    # Region prefix: consult the API so a region missing from the
    # preferred list still walks its OWN zones before cross-region.
    region_zones: list[str] = []
    if project:
        region_zones = _list_region_zones(project, region_or_zone)
    preferred_in_region = [z for z in PREFERRED_ZONES if z in region_zones]
    other_in_region = [z for z in region_zones if z not in preferred_in_region]
    # Fall back to prefix match against the preferred list when the API
    # lookup is unavailable so single-zone pins outside the list still
    # work in offline environments.
    if not region_zones:
        preferred_in_region = [z for z in PREFERRED_ZONES if z.startswith(f"{region_or_zone}-")]
    cross_region = [
        z
        for z in PREFERRED_ZONES
        if z not in preferred_in_region and z not in other_in_region
    ]
    # Zone-capacity contract: intersect with PREFERRED_ZONES when
    # non-empty; only fall back to nonpreferred live zones when the
    # intersection is empty (region missing from the curated capacity
    # list).
    if preferred_in_region:
        candidates = preferred_in_region + cross_region
    else:
        candidates = other_in_region + cross_region
    if not zone_walk:
        candidates = candidates[:1]
    return candidates or list(PREFERRED_ZONES)


# Compute Engine wire wordings for zone-unavailable errors. The
# classifier covers all four observed shapes (sync stockout, async DONE
# with errors, machine-type-not-in-zone, polling-fallback RuntimeError).
_ZONE_TOKENS_CASE_SENSITIVE = ("ZONE_RESOURCE",)
_ZONE_TOKENS_CASE_INSENSITIVE = (
    "stockout",
    "does not have enough resources",
)


def is_zone_unavailable(err: Exception, op: Any = None) -> bool:
    """Return True iff ``err`` (or the optional ``op``) signals zone-unavailable.

    Four shapes:

      1. ``ResourceExhausted`` / 503 sync.
      2. Async op DONE with ZONE_RESOURCE / STOCKOUT in
         ``op.error.errors[].code``.
      3. HTTP 400 ``machineType ... does not exist in zone``.
      4. ``RuntimeError`` from the wait helper joining
         ``code:message`` — matches ZONE_RESOURCE, STOCKOUT, or the
         human-readable "does not have enough resources" sentence.
    """
    if isinstance(err, gax.ResourceExhausted):
        return True
    msg = str(err)
    if "does not exist in zone" in msg and "machineType" in msg:
        return True
    if isinstance(err, RuntimeError):
        if any(tok in msg for tok in _ZONE_TOKENS_CASE_SENSITIVE):
            return True
        msg_upper = msg.upper()
        if any(tok.upper() in msg_upper for tok in _ZONE_TOKENS_CASE_INSENSITIVE):
            return True
    if op is not None and getattr(op, "error", None):
        for e in op.error.errors:
            code = (getattr(e, "code", "") or "").upper()
            if "ZONE_RESOURCE" in code or "STOCKOUT" in code:
                return True
    return False


def delete_failed_zonal_instance(project: str, zone: str, name: str) -> bool:
    """Best-effort delete of a partial async-insert in a failed zone.

    Compute Engine's async DONE-with-errors shape leaves a phantom
    instance record in the failed zone. The multi-zone walker MUST
    call this between zones (``zone_capacity_handling`` shape 2).
    Returns True iff the cleanup
    completed or the record was already absent.
    """
    try:
        op = compute_v1.InstancesClient().delete(project=project, zone=zone, instance=name)
    except gax.NotFound:
        return True
    except gax.GoogleAPICallError as e:
        print(f"  warn: failed-zone cleanup raised: {e}", file=sys.stderr)
        return False
    op_name = getattr(op, "name", None) or getattr(op, "operation", "")
    if op_name:
        try:
            wait_for_zonal_op(project, zone, op_name, timeout=120)
        except Exception as e:
            print(f"  warn: failed-zone cleanup wait raised: {e}", file=sys.stderr)
            return False
    return True


# --------------------------------------------------------------------- #
# Zone / region helpers (region<->zone narrowing)                       #
# --------------------------------------------------------------------- #


def zone_to_region(zone: str) -> str:
    """Strip the trailing zone letter from a zone (us-central1-a -> us-central1)."""
    parts = zone.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha():
        return parts[0]
    return zone


def narrow_region_to_zone(region_or_zone: str, default_zone_letter: str = "a") -> str:
    """Return a zone from either a region or a zone input.

    Compute Engine instance APIs are zone-scoped, but the suite contract
    passes a single ``--region`` argument. Treat already-zone inputs
    (trailing single-letter suffix) as pinned; otherwise append the default
    suffix. The provider config wires ``--zone`` separately so explicit
    pins always win over this fallback.
    """
    parts = region_or_zone.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha():
        return region_or_zone
    return f"{region_or_zone}-{default_zone_letter}"


# --------------------------------------------------------------------- #
# State translation (enumerated emitted set)                            #
# --------------------------------------------------------------------- #


# Mapping from Compute Engine ``Instance.status`` enum values to the
# canonical lifecycle vocabulary used by the suite validators. The emitted
# set is enumerated here so downstream code only branches on documented
# values.
#
# Emitted set: "running", "stopped", "starting", "stopping", "unknown".
#
# TERMINATED in Compute Engine means "stopped" (not deleted); REPAIRING
# is mapped to "unknown" because the guest is unreachable during host-
# failure recovery and treating it as running would weaken AWS provider parity.
# DEPROVISIONING (the post-delete transient) maps to "stopping" so the
# canonical "if state in ('stopping',)" branches downstream see it.
_RAW_TO_CANONICAL: dict[str, str] = {
    "PROVISIONING": "starting",
    "STAGING": "starting",
    "RUNNING": "running",
    "STOPPING": "stopping",
    "STOPPED": "stopped",
    "SUSPENDING": "stopping",
    "SUSPENDED": "stopped",
    "TERMINATED": "stopped",
    "REPAIRING": "unknown",
    "DEPROVISIONING": "stopping",
}


def canonical_state(raw: str | None) -> str:
    """Translate a Compute Engine raw status to the canonical lifecycle vocabulary.

    Emitted set: ``running``, ``stopped``, ``starting``, ``stopping``,
    ``unknown``. Downstream code MUST only branch on these.
    """
    if not raw:
        return "unknown"
    return _RAW_TO_CANONICAL.get(raw.upper(), "unknown")


# --------------------------------------------------------------------- #
# Operation waiters                                                     #
# --------------------------------------------------------------------- #


def wait_for_zonal_op(
    project: str,
    zone: str,
    operation_name: str,
    *,
    timeout: int = 600,
    poll_interval: float = 3,
) -> compute_v1.Operation:
    """Block until a zonal Compute Operation reaches DONE.

    Raises ``RuntimeError`` if the operation's error list is non-empty.
    The joined message includes ``op.error.errors[].code`` so the
    multi-zone walk classifier can match canonical STOCKOUT tokens
    (zone_capacity_handling shape 4).

    ``poll_interval`` is the inter-poll sleep; the 3s default keeps API
    chatter low for slow ops (boot/create), but latency-sensitive callers
    that time a short op window (e.g. the floating-IP switch) pass a tighter
    interval so the measured time is not dominated by poll slop.
    """
    client = compute_v1.ZoneOperationsClient()
    deadline = time.monotonic() + timeout
    while True:
        op = client.get(project=project, zone=zone, operation=operation_name)
        if op.status == compute_v1.Operation.Status.DONE:
            if op.error and op.error.errors:
                msg = "; ".join(f"{getattr(e, 'code', '')}:{getattr(e, 'message', str(e))}" for e in op.error.errors)
                raise RuntimeError(f"Zonal op {operation_name} failed: {msg}")
            return op
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Zonal operation {operation_name} did not complete in {timeout}s")
        time.sleep(poll_interval)


def retry_zonal_lifecycle_op(
    op_fn: Callable[[], Any],
    project: str,
    zone: str,
    *,
    resource_desc: str,
    on_sync_success: Callable[[], None] | None = None,
    attempts: int = 3,
    backoffs: tuple[int, ...] = (60, 120),
    op_timeout: int = 600,
) -> compute_v1.Operation | None:
    """Run a zonal lifecycle op with stockout retry-in-place.

    Lifecycle ops (``instances.start`` / ``stop`` / ``reset``) are
    zone-bound — they
    cannot walk to a different zone on STOCKOUT. The only recovery is
    retry-with-backoff in the same zone, 3 attempts max with 60s / 120s
    backoffs.

    ``op_fn`` must perform the synchronous API call and return the
    ``Operation``. ``on_sync_success`` (if supplied) fires AFTER each
    synchronous return but BEFORE the async wait — callers stamp their
    ``<verb>_initiated`` tracker there so the idempotent-lifecycle
    invariant holds even across retries.

    Stockout shapes covered:
      * Synchronous ``ResourceExhausted`` raise (shape 1).
      * Async DONE-with-errors observed by ``wait_for_zonal_op`` and
        re-raised as ``RuntimeError`` carrying STOCKOUT / ZONE_RESOURCE
        tokens (shapes 2 / 4).

    Non-stockout exceptions re-raise immediately so transient API errors
    do not waste two backoffs.
    """
    last_err: Exception | None = None
    for attempt_idx in range(attempts):
        op: Any = None
        try:
            op = op_fn()
            if on_sync_success is not None:
                on_sync_success()
            op_name = getattr(op, "name", None) or getattr(op, "operation", "")
            if op_name:
                return wait_for_zonal_op(project, zone, op_name, timeout=op_timeout)
            return None
        except Exception as e:
            last_err = e
            if not is_zone_unavailable(e, op=op):
                raise
            if attempt_idx >= attempts - 1:
                break
            wait = backoffs[min(attempt_idx, len(backoffs) - 1)]
            print(
                f"  stockout on {resource_desc} attempt {attempt_idx + 1}/{attempts}; sleeping {wait}s before retry",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise RuntimeError(
        f"Stockout retry exhausted for {resource_desc} after {attempts} attempts: {last_err}"
    ) from last_err


def wait_for_global_op(project: str, operation_name: str, *, timeout: int = 600) -> compute_v1.Operation:
    """Block until a global Compute Operation reaches DONE."""
    client = compute_v1.GlobalOperationsClient()
    deadline = time.monotonic() + timeout
    while True:
        op = client.get(project=project, operation=operation_name)
        if op.status == compute_v1.Operation.Status.DONE:
            if op.error and op.error.errors:
                msg = "; ".join(f"{getattr(e, 'code', '')}:{getattr(e, 'message', str(e))}" for e in op.error.errors)
                raise RuntimeError(f"Global op {operation_name} failed: {msg}")
            return op
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Global operation {operation_name} did not complete in {timeout}s")
        time.sleep(3)


# --------------------------------------------------------------------- #
# Instance polling                                                      #
# --------------------------------------------------------------------- #


def get_instance(project: str, zone: str, name: str) -> compute_v1.Instance:
    """Wrapper around InstancesClient().get for convenience."""
    return compute_v1.InstancesClient().get(project=project, zone=zone, instance=name)


def poll_instance_state(
    project: str,
    zone: str,
    name: str,
    *,
    target_canonical: str,
    timeout: int = 300,
    interval: int = 5,
) -> str:
    """Poll instances.get until canonical_state(status) == target_canonical.

    Returns the final canonical state; raises ``TimeoutError`` on budget
    exhaustion so the caller can record a structured timeout instead of
    silently treating a never-reached state as success.
    """
    deadline = time.monotonic() + timeout
    while True:
        inst = get_instance(project, zone, name)
        cstate = canonical_state(inst.status)
        if cstate == target_canonical:
            return cstate
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Instance {name} did not reach {target_canonical!r} (last={cstate!r}) in {timeout}s")
        time.sleep(interval)


def first_external_ip(instance: compute_v1.Instance) -> str | None:
    """Return ``networkInterfaces[0].accessConfigs[0].natIP`` if present."""
    for nic in instance.network_interfaces:
        for cfg in nic.access_configs:
            ip = getattr(cfg, "nat_i_p", None) or getattr(cfg, "nat_ip", None)
            if ip:
                return ip
    return None


def first_internal_ip(instance: compute_v1.Instance) -> str | None:
    """Return the primary internal IPv4 of ``instance`` (``network_interfaces[0].network_i_p``), or ``None``.

    Returns ``None`` when the instance has no NICs or when nic0's
    ``network_i_p`` is empty (instance still provisioning, or unusual
    no-NIC shape).
    """
    if not instance.network_interfaces:
        return None
    return instance.network_interfaces[0].network_i_p or None


def wait_for_public_ip(
    project: str,
    zone: str,
    name: str,
    *,
    timeout: int = 120,
    interval: int = 5,
) -> str | None:
    """Poll instances.get until an external IP is observable.

    Ephemeral external IPs are released on stop in Compute Engine, so
    post-start / post-reset code MUST re-read rather than rely on a
    cached arg. Terminal classes (NotFound, Unauthenticated,
    PermissionDenied) are re-raised so a wrong instance / wrong zone /
    bad credentials surfaces as a structured error rather than a silent
    timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            inst = get_instance(project, zone, name)
            ip = first_external_ip(inst)
            if ip:
                return ip
        except (gax.ServiceUnavailable, gax.InternalServerError, gax.GatewayTimeout, gax.DeadlineExceeded) as e:
            print(f"  warn: wait_for_public_ip transient: {e}", file=sys.stderr)
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def short_name(self_link: str | None) -> str:
    """Return the trailing path segment from a Compute Engine self-link.

    Use this when verifying scope-binding fields by exact match.
    Substring / startswith comparisons accept supersets and adopt
    resources from the wrong scope.
    """
    if not self_link:
        return ""
    return self_link.rsplit("/", 1)[-1]


def resolve_image(
    image_project: str,
    image_arg: str,
) -> compute_v1.Image:
    """Resolve an operator-supplied image short-name or family to a concrete Image.

    The operator's ``args.project`` (forwarded as ``image_project``
    here) MUST be the FIRST lookup scope —
    vendor-default constants are only an explicit fallback. The resolver
    tries:
        1. ``images.get(project=image_project, image=image_arg)``      (exact name)
        2. ``images.get_from_family(project=image_project, family=image_arg)``
           (family alias)
    Raising the original ``NotFound`` keeps the failure inspectable when
    nothing matches.
    """
    client = compute_v1.ImagesClient()
    try:
        return client.get(project=image_project, image=image_arg)
    except gax.NotFound:
        return client.get_from_family(project=image_project, family=image_arg)


# --------------------------------------------------------------------- #
# Tag/label projection                                                  #
# --------------------------------------------------------------------- #


# Compute Engine label keys must match [a-z]([-a-z0-9_]*). The suite
# expects canonical mixed-case Name / CreatedBy keys; we project to
# api-valid forms on create and back on read so InstanceTagCheck stays
# unchanged.
_TAG_TO_LABEL: dict[str, str] = {
    "Name": "isv_name",
    "CreatedBy": "createdby",
}
_LABEL_TO_TAG: dict[str, str] = {v: k for k, v in _TAG_TO_LABEL.items()}


def canonical_tags_to_labels(tags: dict[str, str]) -> dict[str, str]:
    """Project canonical Name/CreatedBy tag keys to api-valid Compute Engine labels."""
    out: dict[str, str] = {}
    for k, v in tags.items():
        label_key = _TAG_TO_LABEL.get(k, k.lower())
        label_val = re.sub(r"[^a-z0-9_-]", "-", (v or "").lower())[:63]
        out[label_key] = label_val
    return out


def labels_to_canonical_tags(labels: dict[str, str] | None) -> dict[str, str]:
    """Project Compute Engine labels back to canonical suite tag names."""
    if not labels:
        return {}
    out: dict[str, str] = {}
    for k, v in labels.items():
        out[_LABEL_TO_TAG.get(k, k)] = v
    return out


# --------------------------------------------------------------------- #
# Local SSH key pair (verified-reuse, returns created bool)             #
# --------------------------------------------------------------------- #


_KEY_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,255}")


def sanitize_key_name(key_name: str) -> str:
    """Reject key names that could escape /tmp when composed into a path."""
    if not key_name or not _KEY_NAME_RE.fullmatch(key_name):
        raise ValueError(f"invalid key name {key_name!r}: must match [A-Za-z0-9_.-] (1-255 chars).")
    return key_name


def read_ssh_pubkey(priv_path: str) -> str:
    """Read the OpenSSH public-key line that pairs with ``priv_path``.

    ``ssh-keygen`` derives ``<priv>.pub`` from the FULL private path, so a
    private key at ``isv-test-key.pem`` produces ``isv-test-key.pem.pub``.
    The pairing lives in one place — local artifacts count as resources too.
    """
    return Path(f"{priv_path}.pub").read_text().strip()


def generate_ssh_keypair(
    key_name: str,
    key_dir: str | Path | None = None,
) -> tuple[str, bool]:
    """Generate or verified-reuse a local OpenSSH key pair.

    Compute Engine has no managed key-pair store; the SSH public key is
    attached via instance metadata. The local PEM + ``.pub`` pair is the
    artifact that survives the run, so it must follow the verified-reuse
    cleanup contract:

      * Returns ``(private_key_path, created)``.
      * ``created`` is True only when this call generated a fresh pair.
      * An adopted pair (both files non-empty) returns ``created=False`` so
        teardown skips local deletion.
    """
    sanitize_key_name(key_name)
    base = Path(key_dir) if key_dir else Path("/tmp")
    base.mkdir(parents=True, exist_ok=True)
    priv = base / f"{key_name}.pem"
    pub = base / f"{key_name}.pem.pub"

    if priv.exists() and pub.exists() and priv.stat().st_size > 0 and pub.stat().st_size > 0:
        print(f"  reusing existing local SSH key pair: {priv}", file=sys.stderr)
        return str(priv), False

    # Wipe partial state and regenerate.
    for p in (priv, pub):
        if p.exists():
            try:
                p.chmod(0o600)
            except OSError:
                pass
            p.unlink(missing_ok=True)

    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "2048", "-N", "", "-q", "-f", str(priv)],
        check=True,
    )
    priv.chmod(0o400)
    print(f"  generated SSH key pair: {priv}", file=sys.stderr)
    return str(priv), True


def delete_local_keypair(priv_path: str) -> bool:
    """Remove the local PEM + ``.pub`` pair. Returns True iff both files are gone."""
    ok = True
    for p in (Path(priv_path), Path(priv_path + ".pub")):
        try:
            if p.exists():
                p.chmod(0o600)
                p.unlink()
        except OSError as e:
            print(f"  warn: could not remove {p}: {e}", file=sys.stderr)
            ok = False
    return ok


# --------------------------------------------------------------------- #
# Firewall (verified-reuse, returns created bool)                       #
# --------------------------------------------------------------------- #


# Compute Engine firewall rules do NOT accept a labels field on the proto.
# The closest analog to the AWS provider's CreatedBy=isvtest tag-based
# check is to embed
# the ownership marker in the rule's description and require an exact
# match on adopt.
_ISV_OWNERSHIP_MARKER = "createdby=isvtest"
_ISV_FIREWALL_DESCRIPTION = f"ISV validation SSH firewall rule ({_ISV_OWNERSHIP_MARKER})"
ISV_NETWORK_TAG = "isv-test-vm"


def _firewall_matches_ssh_shape(rule: compute_v1.Firewall, network_short: str) -> bool:
    """Return True iff the existing firewall rule matches the SSH-allow shape.

    Rule #7 invariant: every caller-depended property must be verified on
    reuse-adoption. A rule with the ownership marker + description + port
    shape but ``disabled=True`` would be silently adopted, then SSH would
    time out at cloud-init wait and teardown would skip the rule (because
    ``firewall_created=False``). Reject disabled rules up front.
    """
    if rule.disabled:
        return False
    if short_name(rule.network) != network_short:
        return False
    if rule.direction != "INGRESS":
        return False
    # Rule #7 requires every caller-depended invariant be verified on
    # reuse-adoption. Use set-equality (not membership) so a rule with
    # extra CIDRs (`["0.0.0.0/0", "10.0.0.0/8"]`) or extra target tags
    # is REJECTED rather than silently adopted with `firewall_created=
    # False` — adopting a superset rule weakens the test contract AND
    # persists post-run because teardown gates on the `firewall_created`
    # flag.
    if set(rule.source_ranges) != {"0.0.0.0/0"}:
        return False
    if set(rule.target_tags) != {ISV_NETWORK_TAG}:
        return False
    # Require exactly one allowed entry: tcp/22. Multiple entries or
    # additional ports broaden the ingress beyond the caller-declared
    # scope and must be rejected.
    if len(rule.allowed) != 1:
        return False
    allowed = rule.allowed[0]
    if allowed.I_p_protocol.lower() != "tcp":
        return False
    if list(allowed.ports) != ["22"]:
        return False
    return True


def _firewall_has_isv_ownership(rule: compute_v1.Firewall) -> bool:
    """Return ``True`` iff ``rule.description`` carries the ISV ownership marker.

    Used by verified-reuse to distinguish firewalls that this suite
    created (and is therefore safe to mutate / delete) from operator-
    owned firewalls that happen to match the test's name pattern.
    """
    return _ISV_OWNERSHIP_MARKER in (rule.description or "").lower()


def insert_ssh_firewall(
    project: str,
    name: str,
    network_short: str,
) -> tuple[str, Any]:
    """Submit a verified-reuse SSH firewall insert and return ``(name, op)``.

    Stamp-before-wait split of the previous ``ensure_ssh_firewall``
    (cleanup-tracker sub-rule): the caller stamps
    ``firewall_created=True`` IMMEDIATELY after this
    function returns ``op != None``, BEFORE running ``wait_for_global_op``.
    A post-insert wait failure then leaves the caller with the truthful
    ``firewall_created=True`` so cleanup-on-failure deletes the
    accepted-but-uncomfirmed rule.

    Returns ``(name, op)``. ``op`` is ``None`` iff the call adopted a
    verified-reuse existing rule (no wait required); otherwise the
    caller MUST block on ``wait_for_global_op(project, op.name, ...)``.

    Adoption is gated on the same three checks as before: ownership
    marker present, description matches what we'd produce, shape
    matches (network/direction/source/target tag/tcp22).
    """
    fw_client = compute_v1.FirewallsClient()
    network_url = f"projects/{project}/global/networks/{network_short}"

    rule = compute_v1.Firewall()
    rule.name = name
    rule.network = network_url
    rule.direction = "INGRESS"
    rule.priority = 1000
    rule.source_ranges = ["0.0.0.0/0"]
    rule.target_tags = [ISV_NETWORK_TAG]
    rule.description = _ISV_FIREWALL_DESCRIPTION

    allowed = compute_v1.Allowed()
    allowed.I_p_protocol = "tcp"
    allowed.ports = ["22"]
    rule.allowed = [allowed]

    try:
        op = fw_client.insert(project=project, firewall_resource=rule)
    except gax.Conflict:
        existing = fw_client.get(project=project, firewall=name)
        if not _firewall_has_isv_ownership(existing):
            raise RuntimeError(
                f"firewall {name!r} exists in {project} without ownership marker "
                f"{_ISV_OWNERSHIP_MARKER!r}; refusing to adopt"
            ) from None
        if (existing.description or "") != _ISV_FIREWALL_DESCRIPTION:
            raise RuntimeError(
                f"firewall {name!r} description differs: expected "
                f"{_ISV_FIREWALL_DESCRIPTION!r}, got {existing.description!r}"
            ) from None
        if not _firewall_matches_ssh_shape(existing, network_short):
            raise RuntimeError(
                f"firewall {name!r} exists but shape (network/direction/source/tags/tcp22) "
                "does not match; refusing to adopt"
            ) from None
        print(f"  reusing verified firewall rule: {name}", file=sys.stderr)
        return name, None

    print(f"  inserted firewall rule {name} (op pending)", file=sys.stderr)
    return name, op
