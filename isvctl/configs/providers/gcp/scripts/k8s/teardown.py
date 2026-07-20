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

"""Destroy the primary GKE cluster (teardown phase).

Exact-ownership destroy via the threaded cluster state (the RUN_ID-suffixed
cluster setup created); a var-less `terraform destroy` re-derives the same
required inputs from env/state so it never aborts "No value for required
variable". Idempotent: an already-gone cluster is success. Honors the
--skip-destroy flag (GCP_K8S_SKIP_TEARDOWN=true) so an operator can keep the
cluster for debugging.

Two GKE-specific teardown steps beyond the EKS oracle:
  1. Reclaim run-created PVC-backed Persistent Disks BEFORE the cluster is
     destroyed — a GKE cluster delete does NOT reclaim them, so they orphan as
     standalone Compute disks (the released NIM/CSI checks create such PVCs).
  2. `terraform init` runs UNCONDITIONALLY (never gated on `.terraform`) so a
     teardown-on-failure after setup bailed early (stale/absent lock) still
     reconciles the lock and no-ops cleanly instead of aborting "Inconsistent
     dependency lock file".
  3. BACKSTOP: after the cluster is gone, reclaim this run's leaked PVC-backed
     Persistent Disks. A `goog-k8s-cluster-name` label match DISCOVERS surviving
     candidate disks zone-agnostically, but a delete is AUTHORIZED only when the
     disk is in this run's FULL-run ownership ledger — the exact disk identities
     captured from live PVs while the cluster was up and its ownership marker was
     verified. The truncated cluster label (8 identity chars) is NOT ownership
     proof on its own: two runs whose RUN_IDs share the first 8 chars collide on
     it, so a label-only delete could reap a prefix-colliding run's detached
     orphan disk. A failed ledger/disk listing, a surviving ledger-owned disk, or
     a cluster-labeled disk with unprovable full-run ownership is surfaced as
     `cleanup_errors` with `success=False` — a leaked billable disk never presents
     as a clean teardown.
  4. BACKSTOP: reclaim any GPU capacity-preflight probe MIG / instance-template
     this run left behind (setup + create_test_gpu_node_pool each probe), scoped
     to THIS run's full-identity probe ledger. A probe MIG is a standalone billable
     size-1 GPU resource outside Terraform state and without the cluster label, so a
     failed inline probe delete would otherwise leak silently; an unconfirmed
     reclaim is surfaced as `cleanup_errors` with `success=False` too. This probe
     backstop is scoped purely to the run id (independent of any cluster state), so
     it runs on EVERY non-preservation path — including when no cluster state
     exists (a setup GPU-preflight bail can leave a probe MIG without ever writing
     cluster state), after a failed `terraform destroy` (each backstop runs in its
     own guarded block, so a destroy raise no longer skips the reclaim), and after
     ANY preflight raise past the guarded blocks once the project is resolved (a
     `terraform init`, state-classify, or ownership-describe failure jumps to the
     outer handler, which still runs the probe backstop rather than exiting with a
     billable probe unreclaimed).

AWS reference: ../../aws/scripts/eks/teardown.sh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_lib as k8s

PLATFORM = "kubernetes"
_DESTROY_TIMEOUT = 2400
# Confirmation poll AFTER terraform destroy: terraform's delete OPERATION completing is
# not proof of live absence, so poll the exact cluster until the API returns not_found
# before any dependent orphan-disk cleanup runs or success is reported. Kept well under
# the teardown step cap (3000s) on top of _DESTROY_TIMEOUT; a genuine destroy already
# blocked on the delete LRO, so this returns on the first poll in the common case.
_ABSENCE_WAIT_TIMEOUT = 300
_PLACEHOLDER = "placeholder"
_PLACEHOLDER_ZONE = "us-central1-a"


def _pd_reclaim_errors(reclaim: dict[str, Any], cluster_name: str) -> list[str]:
    """Cleanup-error strings for any run-owned Persistent Disk that could not be
    confirmed reclaimed (a failed ledger/disk listing, a surviving ledger-owned
    disk, or a cluster-labeled disk whose full-run ownership could not be proven).
    Empty when the backstop confirmed every run-owned disk is gone."""
    errors: list[str] = []
    if reclaim["list_error"]:
        errors.append(
            "could not list run-owned Persistent Disks (label "
            f"goog-k8s-cluster-name={cluster_name}); orphaned disks may remain: {reclaim['list_error']}"
        )
    if reclaim["failed"]:
        errors.append(f"failed to delete run-owned Persistent Disk(s): {', '.join(reclaim['failed'])}")
    if reclaim.get("unverified"):
        # A disk carrying THIS run's truncated goog-k8s-cluster-name label whose full-run
        # ownership could not be proven (no completed live disk-identity capture). It is
        # never deleted from the truncated label alone, but a possibly-leaked billable disk
        # must never present as a clean teardown, so surface it for manual review.
        errors.append(
            "found Persistent Disk(s) carrying this run's truncated cluster label whose full-run "
            "ownership could NOT be verified (no completed live disk-identity capture); refusing to "
            f"delete from the truncated label alone: {', '.join(reclaim['unverified'])}"
        )
    return errors


def _probe_reclaim_errors(probe_reclaim: dict[str, Any]) -> list[str]:
    """Cleanup-error strings for any run-owned GPU capacity-preflight probe that
    could not be confirmed reclaimed. A retained probe MIG is a standalone billable
    size-1 GPU resource, so an unconfirmed reclaim must never present as clean."""
    errors: list[str] = []
    if probe_reclaim["list_error"]:
        errors.append(
            "could not list run-owned GPU-preflight probe resources; a billable "
            f"probe MIG may remain: {probe_reclaim['list_error']}"
        )
    if probe_reclaim["failed"]:
        errors.append(
            f"failed to delete run-owned GPU-preflight probe resource(s): {', '.join(probe_reclaim['failed'])}"
        )
    return errors


def _reclaim_orphan_pds(
    project: str, cluster_name: str, *, capture_fresh: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """Run the run-scoped orphan-PD backstop in its own guarded block so it never
    raises past teardown (an unexpected failure becomes a cleanup-error string
    instead of masking the primary result).

    ``capture_fresh`` threads THIS teardown's own live-PV capture result: only a fresh,
    complete, persisted capture authorizes treating an out-of-ledger cluster-labeled disk as
    a prefix-colliding OTHER run's. It defaults to ``False`` (fail closed) so a path that ran
    no live capture — e.g. the absent/valid-empty stateless reconcile — surfaces every
    unmatched disk as ``unverified`` rather than trusting an older completed ledger."""
    try:
        reclaim = k8s.delete_orphan_pds(project, cluster_name, capture_fresh=capture_fresh)
    except BaseException as exc:  # a backstop must never crash the teardown
        reclaim = {"deleted": [], "failed": [], "unverified": [], "list_error": f"disk reclaim raised: {exc}"}
    return reclaim, _pd_reclaim_errors(reclaim, cluster_name)


def _reclaim_gpu_probes(project: str) -> tuple[dict[str, Any], list[str]]:
    """Run the run-scoped GPU capacity-probe backstop in its own guarded block. The
    backstop is derived purely from this run's id, independent of any cluster
    Terraform state, so it is the one reclaim that must run even when no cluster
    state exists. Never raises past teardown."""
    try:
        probe_reclaim = k8s.delete_orphan_gpu_probes(project)
    except BaseException as exc:  # a backstop must never crash the teardown
        probe_reclaim = {"deleted": [], "failed": [], "list_error": f"probe reclaim raised: {exc}"}
    return probe_reclaim, _probe_reclaim_errors(probe_reclaim)


def _emit_preserved_teardown() -> int:
    """Preservation path (--skip-destroy): keep the cluster + node pools, but still
    reclaim a leaked GPU capacity-preflight probe MIG.

    A probe MIG is a STANDALONE billable size-1 GPU resource OUTSIDE the cluster,
    so preserving the cluster does not preserve it. If an inline probe delete was
    left unconfirmed during setup, a plain skip-teardown would present a
    success-shaped result while that MIG bills forever. Run the probe-ONLY backstop
    (never touching the cluster or node pools) when a reclaim is PENDING, and make
    its outcome part of the structured result. When nothing is pending, keep the
    fast no-auth short-circuit so the common preservation case stays cheap."""
    if not k8s.retained_probes_pending():
        return k8s.emit(
            {
                "success": True,
                "platform": PLATFORM,
                "skipped": True,
                "message": (
                    "Teardown skipped (GCP_K8S_SKIP_TEARDOWN=true); GKE cluster preserved; "
                    "no GPU capacity-preflight probe cleanup pending."
                ),
            }
        )

    result: dict[str, Any] = {"success": False, "platform": PLATFORM, "skipped": True}
    try:
        project = k8s.resolve_project_id()
        probe_reclaim, cleanup_errors = _reclaim_gpu_probes(project)
        if cleanup_errors:
            result.update(
                {
                    "success": False,
                    "error_type": "cleanup_incomplete",
                    "error": (
                        "[bucket=cleanup_incomplete] Cluster preserved (GCP_K8S_SKIP_TEARDOWN=true), "
                        "but run-owned GPU capacity-preflight probe reclaim is UNCONFIRMED: "
                        + "; ".join(cleanup_errors)
                    ),
                    "resources_deleted": [],
                    "cleanup_errors": cleanup_errors,
                }
            )
        else:
            # Marker cleared only once every retained probe is confirmed gone, so a
            # rerun re-attempts the backstop while any leak remains.
            k8s.clear_retained_probes_marker()
            result.update(
                {
                    "success": True,
                    "message": (
                        "Teardown skipped (GCP_K8S_SKIP_TEARDOWN=true); GKE cluster + node pools "
                        f"preserved; {len(probe_reclaim['deleted'])} retained GPU capacity-preflight "
                        "probe resource(s) reclaimed."
                    ),
                    "resources_deleted": [],
                }
            )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc, skipped=True)
    return k8s.emit(result)


def _emit_stateless_teardown(project: str, cluster_name: str, location: str, state_file: str) -> int:
    """Absent / valid-empty primary-cluster state: reconcile the exact deterministic
    cluster live BEFORE reporting clean, then run the run-scoped GPU-probe backstop.

    Setup can bail before a durable apply (a timed-out / interrupted apply can still
    leave a billable cluster whose state address never became durable), and a prior
    teardown leaves a valid-empty state — file presence alone must not report "nothing
    to destroy". Describe the exact cluster in the known deterministic project/location:
    a confirmed-absent cluster is clean; a run-owned leak is imported + destroyed +
    waited to confirmed absence (then its cluster-labeled orphan PDs reclaimed); an
    unreadable or mismatched ownership marker fails visibly and leaves the cluster
    untouched. The run-scoped GPU-probe backstop runs regardless — it is independent of
    cluster state."""
    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        tf_vars = {
            "project": project,
            "cluster_name": cluster_name,
            "location": location,
            # Delete-irrelevant vars (targeted by state) take benign placeholders.
            "system_machine_type": _PLACEHOLDER,
            "gpu_machine_type": _PLACEHOLDER,
            "gpu_accelerator_type": _PLACEHOLDER,
            "gpu_node_locations": [_PLACEHOLDER_ZONE],
        }
        try:
            cluster_outcome = k8s.reconcile_orphaned_cluster(
                k8s.CLUSTER_TF_DIR,
                state_file,
                "google_container_cluster.primary",
                cluster_name,
                location,
                project,
                tf_vars,
                destroy_timeout=_DESTROY_TIMEOUT,
            )
        except k8s.LifecycleError as exc:
            # Ownership unreadable/mismatched, or the reconcile destroy failed: never a
            # false clean. Still run the run-scoped probe backstop and surface both.
            _probe_reclaim, probe_errors = _reclaim_gpu_probes(project)
            cleanup_errors = [
                f"refused to reconcile cluster {cluster_name} from absent/valid-empty state: {exc.detail}"
            ] + probe_errors
            result.update(
                {
                    "success": False,
                    "error_type": exc.bucket,
                    "error": exc.detail,
                    "resources_deleted": [],
                    "cleanup_errors": cleanup_errors,
                }
            )
            return k8s.emit(result)

        # Cluster reconciled. BOTH outcomes mean the exact deterministic cluster is
        # CONFIRMED gone: "reclaimed" (a run-owned leak was imported, destroyed, and
        # waited to absence) or "absent" (already confirmed not-found live). A GKE
        # cluster delete does NOT reclaim its PVC-backed Persistent Disks, so a
        # run-owned billable disk can survive on EITHER confirmed-gone branch — e.g. a
        # prior tracked-path teardown destroyed the cluster (state became valid-empty)
        # but its PD reclaim hit a transient list error, and this rerun classifies the
        # cluster as confirmed-absent. Run the PD backstop on both so neither branch can
        # present a billable disk leak as a clean teardown. It is safe on the confirmed-
        # absent branch too: delete_orphan_pds authorizes a delete only for a disk in THIS
        # run's full-run ownership ledger (a `goog-k8s-cluster-name` label match merely
        # discovers candidates), and requires each disk be described and detached before
        # deletion, so it never touches a foreign, prefix-colliding, or in-use disk — an
        # unprovable cluster-labeled disk is surfaced, never reaped. This stateless path
        # ran no live capture, so a genuine leak here surfaces as `unverified` rather than
        # being deleted from the truncated label alone. Run the GPU-probe backstop alongside.
        probe_reclaim, probe_errors = _reclaim_gpu_probes(project)
        pd_reclaim, pd_errors = _reclaim_orphan_pds(project, cluster_name)
        cleanup_errors = pd_errors + probe_errors

        if cleanup_errors:
            result.update(
                {
                    "success": False,
                    "error_type": "cleanup_incomplete",
                    "error": (
                        "[bucket=cleanup_incomplete] primary-cluster state was absent/valid-empty and "
                        "the exact cluster was reconciled, but run-owned billable resource reclaim is "
                        "UNCONFIRMED: " + "; ".join(cleanup_errors)
                    ),
                    "resources_deleted": ["google_container_cluster"] if cluster_outcome == "reclaimed" else [],
                    "cleanup_errors": cleanup_errors,
                }
            )
        else:
            k8s.clear_retained_probes_marker()
            if cluster_outcome == "reclaimed":
                message = (
                    f"Primary cluster {cluster_name} had no durable state but was found live and "
                    "run-owned (an ambiguous create); imported, destroyed, and confirmed absent; "
                    f"{len(pd_reclaim['deleted'])} orphaned PD(s) and "
                    f"{len(probe_reclaim['deleted'])} GPU-preflight probe resource(s) reclaimed."
                )
                resources_deleted = ["google_container_cluster", "google_container_node_pool"]
            else:
                message = (
                    f"Cluster state {state_file} absent/valid-empty and cluster {cluster_name} "
                    "confirmed absent live - no cluster to destroy; "
                    f"{len(pd_reclaim['deleted'])} orphaned PD(s) and "
                    f"{len(probe_reclaim['deleted'])} GPU-preflight probe resource(s) reclaimed."
                )
                resources_deleted = []
            result.update(
                {
                    "success": True,
                    "message": message,
                    "resources_deleted": resources_deleted,
                }
            )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        # Same preflight-isolated cleanup invariant as main(): a raise BEFORE the guarded
        # probe reclaim above (e.g. reconcile_orphaned_cluster raising a non-LifecycleError,
        # or building tf_vars) must still reclaim the run-id-scoped GPU probe MIG. `project`
        # is always resolved here (a parameter), so run the probe backstop and merge any
        # failure into cleanup_errors while keeping the original error primary.
        result = k8s.error_result(PLATFORM, exc)
        _probe_reclaim, probe_errors = _reclaim_gpu_probes(project)
        if probe_errors:
            result["cleanup_errors"] = list(result.get("cleanup_errors", [])) + probe_errors
    return k8s.emit(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Destroy the primary GKE cluster via Terraform.")
    parser.add_argument("--cluster-name", default="isv-gke", help="Cluster name base (RUN_ID-suffixed by the stub).")
    parser.add_argument(
        "--location",
        required=True,
        help="Cluster location (deterministic; used to describe the exact cluster for "
        "the absent/valid-empty-state live reconcile before reporting clean).",
    )
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        help="Preserve the cluster for debugging (GCP_K8S_SKIP_TEARDOWN=true).",
    )
    args = parser.parse_args()

    if args.skip_destroy:
        return _emit_preserved_teardown()

    project: str | None = None
    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        project = k8s.resolve_project_id()
        cluster_name = k8s.scoped_name(args.cluster_name)
        state_file = k8s.cluster_state_file()

        # Initialize unconditionally (idempotent; reconciles a stale lock) so
        # `terraform state list` classification and any reconcile/destroy below read a
        # ready local backend, then classify the primary cluster's state by its EXACT
        # address rather than mere file presence.
        k8s.terraform_init(k8s.CLUSTER_TF_DIR)
        state_class = k8s.classify_state(k8s.CLUSTER_TF_DIR, state_file, "google_container_cluster.primary")

        if state_class == "unreadable":
            # State present but `terraform state list` failed: ownership/identity cannot
            # be classified. Fail CLOSED (ownership_unprovable). Still run the
            # run-scoped GPU-probe backstop (independent of cluster state).
            _probe_reclaim, probe_errors = _reclaim_gpu_probes(project)
            cleanup_errors = [
                f"refused to destroy cluster {cluster_name}: its Terraform state {state_file} exists "
                "but `terraform state list` could not be read, so its provenance is unprovable"
            ] + probe_errors
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_unprovable",
                    "error": (
                        f"[bucket=ownership_unprovable] refusing to destroy cluster {cluster_name}: its "
                        f"Terraform state {state_file} exists but `terraform state list` is unreadable, "
                        "so ownership cannot be classified. The cluster was left untouched; only "
                        "run-id-scoped GPU-probe reclaim ran."
                    ),
                    "resources_deleted": [],
                    "cleanup_errors": cleanup_errors,
                }
            )
            return k8s.emit(result)

        if state_class in ("absent", "empty"):
            # No tracked cluster address: setup bailed before a durable `terraform
            # apply` (an ambiguous create can still leave a billable cluster), OR a
            # prior teardown already destroyed it (a valid-empty state). Reconcile the
            # exact deterministic cluster live before reporting clean, and run the
            # run-scoped GPU-probe backstop — a retained billable cluster or probe MIG
            # can never present as a clean "nothing to destroy".
            return _emit_stateless_teardown(project, cluster_name, args.location, state_file)

        # state_class == "tracked": the cluster address is in state.
        # Recover the create-time location from state — REQUIRED to describe the LIVE
        # cluster and prove ownership before ANY destroy. It must NOT fall back to a
        # placeholder: a wrong location would describe a DIFFERENT resource and read a
        # false not_found, which would then authorize destroying a state-targeted
        # cluster whose ownership was never proven. If it is unreadable, fail CLOSED —
        # refuse the destroy as a visible structured failure and reclaim only the
        # run-id-scoped GPU probe, whose ownership does not depend on the cluster.
        try:
            location = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, state_file, "location")
        except k8s.LifecycleError as exc:
            k8s.log(f"warning: skipping cluster destroy — create-time location unreadable: {exc.detail}")
            _probe_reclaim, probe_errors = _reclaim_gpu_probes(project)
            cleanup_errors = [
                f"refused to destroy cluster {cluster_name}: create-time location is unreadable from "
                f"state ({exc.detail}); run ownership cannot be verified"
            ] + probe_errors
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_unprovable",
                    "error": (
                        "[bucket=ownership_unprovable] refusing to destroy a cluster whose create-time "
                        f"location is unreadable from state ({exc.detail}); run ownership cannot be "
                        "proven. The cluster and its PVC-backed disks were left untouched; only "
                        "run-id-scoped GPU-probe reclaim ran."
                    ),
                    "resources_deleted": [],
                    "cleanup_errors": cleanup_errors,
                }
            )
            return k8s.emit(result)
        # gpu_zone feeds only the delete-irrelevant gpu_node_locations tf var (the
        # destroy is state-targeted), so a placeholder fallback here never gates
        # ownership and is harmless.
        try:
            gpu_zone = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, state_file, "gpu_zone")
        except k8s.LifecycleError:
            gpu_zone = _PLACEHOLDER_ZONE

        # 1) OWNERSHIP GATE FIRST — before ANY cluster-scoped mutation (PVC reclaim,
        #    terraform destroy, OR the cluster-labeled PD reclaim). The state only
        #    ever holds resources this run CREATED or adopted with PROVEN
        #    full-identity ownership (setup/create_node_pool verify the cloud-side
        #    marker before importing). As a fail-closed backstop against a state
        #    entry that now resolves to a deleted-and-replaced same-name FOREIGN
        #    cluster, re-verify the LIVE ownership marker up front. destroy_ownership_ok
        #    returns ok=True ONLY on positively proven ownership — a live marker that
        #    matches this run, or a clean not_found (an idempotent no-op reconcile).
        #    cluster_destroy_disposition returns a 3-way disposition: "owned" (live
        #    marker matches this run), "absent" (a clean not_found — already gone, an
        #    idempotent no-op reconcile), or "unproven" on EVERY unproven outcome (a
        #    marker present-but-a-different-run, a marker absent on a live cluster, OR a
        #    marker that is UNREADABLE (auth / permission / transport / malformed
        #    describe)). A describe flake therefore NEVER authorizes destroying a
        #    same-name cluster we cannot prove we own — teardown fails visibly and a
        #    rerun with a readable marker recovers. The "absent" disposition is kept
        #    DISTINCT from "owned" so the confirmed-absent branch below SKIPS the
        #    impossible live-cluster PV capture instead of forcing that expected capture
        #    failure into a false cleanup_incomplete.
        disposition, ownership_reason = k8s.cluster_destroy_disposition(cluster_name, location, project)
        cluster_confirmed_absent = disposition == "absent"

        if disposition == "unproven":
            # Ownership not proven: our state may point at a foreign/replaced same-name
            # cluster, or the ownership marker is unreadable. Either way NEVER touch
            # its PVCs, its cluster, or its cluster-labeled disks (those may belong to
            # a replacement). Reclaim ONLY the run-id-scoped GPU capacity-preflight
            # probe — it is scoped purely to THIS run's id, independent of any cluster
            # — and surface the ownership anomaly as a VISIBLE failure. An unproven or
            # foreign-cluster preserve must never present as a clean "destroyed".
            k8s.log(f"warning: skipping cluster destroy — {ownership_reason}")
            _probe_reclaim, probe_errors = _reclaim_gpu_probes(project)
            cleanup_errors = [f"refused to destroy cluster {cluster_name}: {ownership_reason}"] + probe_errors
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_conflict",
                    "error": (
                        "[bucket=ownership_conflict] refusing to destroy a cluster this run does not "
                        f"own: {ownership_reason}. The cluster and its PVC-backed disks were left "
                        "untouched; only run-id-scoped GPU-probe reclaim ran."
                    ),
                    "resources_deleted": [],
                    "cleanup_errors": cleanup_errors,
                }
            )
            return k8s.emit(result)

        # disposition is "owned" (the live ownership marker matched this run) OR "absent"
        # (the tracked cluster is CONFIRMED not_found live). Both permit the state-targeted
        # destroy; the confirmed-absent branch skips the impossible live-cluster PV capture.
        # 2) Reclaim run PVCs while the cluster still lives (best-effort). FIRST capture
        #    the EXACT identities of this run's PVC-backed Persistent Disks into the
        #    full-run ownership ledger — this is the only window a standalone disk can be
        #    tied back to the FULL RUN_ID (the cluster is live and its ownership marker was
        #    just verified above). The truncated goog-k8s-cluster-name label alone is NOT
        #    ownership proof, so the PD backstop authorizes a delete only for a disk in this
        #    ledger. Capture BEFORE deleting the PVCs so every potentially-orphaned disk is
        #    recorded; the capture never raises (a failure records an incomplete ledger AND
        #    returns complete=False so the backstop fails closed and the failure is surfaced).
        #    Bind BOTH the capture and the destructive `kubectl delete pvc --all` to an
        #    ISOLATED, target-validated kubeconfig for THIS exact ownership-verified cluster:
        #    a concurrent run flipping the shared ambient current-context can otherwise make
        #    our capture ledger a foreign cluster's disks or our delete wipe another live
        #    cluster's PVCs.
        capture_status: dict[str, Any] = {
            "complete": False,
            "error": "run-owned Persistent Disk identity capture did not run (cluster became "
            "unreachable before capture)",
        }
        teardown_kubeconfig: Path | None = None
        if cluster_confirmed_absent:
            # The tracked cluster is CONFIRMED absent live, so kubeconfig-based live-PV
            # capture is IMPOSSIBLE — there is no cluster to reach. Skip it and rely on the
            # DURABLE PD ownership ledger (persisted by an earlier attempt's live capture)
            # + the run-scoped probe backstop below. Mark the capture skipped-by-absence
            # (an EXPECTED, non-error condition — NOT a capture failure) so a legitimately
            # already-gone cluster is never forced into a false cleanup_incomplete. Because
            # complete stays False the PD backstop still runs capture_fresh=False, surfacing
            # any out-of-ledger cluster-labeled disk as `unverified` rather than trusting a
            # stale ledger, so it continues to fail closed on an unverifiable survivor.
            capture_status = {"complete": False, "absent": True, "error": ""}
        else:
            try:
                teardown_kubeconfig = k8s.isolated_kubeconfig_for(cluster_name, location, project)
                capture_status = k8s.record_owned_pds_from_live_pvs(cluster_name, teardown_kubeconfig)
                k8s.reclaim_run_pvcs(teardown_kubeconfig)
            except k8s.LifecycleError as exc:
                k8s.log(f"warning: PVC reclaim skipped (cluster may be unreachable): {exc.detail}")
                capture_status = {"complete": False, "error": exc.detail}
            finally:
                if teardown_kubeconfig is not None:
                    k8s.discard_isolated_kubeconfig(teardown_kubeconfig)

        # 3) terraform destroy the cluster (init unconditionally first). Capture a
        #    destroy failure instead of letting it jump straight to the outer
        #    handler: the run-scoped PD + GPU-probe backstops below reclaim
        #    standalone billable resources terraform never owns, so they MUST run on
        #    every exit — including a failed destroy. The destroy error stays the
        #    primary reported failure; the backstops never mask it.
        destroy_error: BaseException | None = None
        resources_deleted: list[str] = []
        try:
            k8s.terraform_init(k8s.CLUSTER_TF_DIR)
            tf_vars = {
                "project": project,
                "cluster_name": cluster_name,
                "location": location,
                # Delete-irrelevant vars (targeted by state) take benign placeholders.
                "system_machine_type": _PLACEHOLDER,
                "gpu_machine_type": _PLACEHOLDER,
                "gpu_accelerator_type": _PLACEHOLDER,
                "gpu_node_locations": [gpu_zone],
            }
            k8s.terraform_destroy(k8s.CLUSTER_TF_DIR, state_file, tf_vars, timeout=_DESTROY_TIMEOUT)
            # terraform destroy returns once the delete OPERATION is accepted, which is
            # NOT proof of live absence (wait_cluster_absent documents this). Poll the
            # EXACT cluster until the API returns not_found BEFORE the dependent
            # orphan-disk backstop runs or success is reported — otherwise orphan-disk
            # cleanup could begin while the cluster is still live, or teardown could
            # report a cluster reclaimed while it remains observable and billable. A
            # timeout / unreadable describe RAISES here and becomes destroy_error below,
            # which skips the PD backstop (the cluster may still be live) and fails
            # visibly. On the confirmed-absent branch this returns on the first poll.
            k8s.wait_cluster_absent(cluster_name, location, project, timeout=_ABSENCE_WAIT_TIMEOUT)
            # On the confirmed-absent branch the destroy only reconciled stale tracked
            # state (no live cluster was deleted), so do not claim a cluster deletion.
            resources_deleted = (
                [] if cluster_confirmed_absent else ["google_container_cluster", "google_container_node_pool"]
            )
        except BaseException as exc:
            destroy_error = exc

        # 4) Backstop (always safe): reclaim any GPU capacity-preflight probe MIG /
        #    template this run left behind (setup + create_test_gpu_node_pool each
        #    probe). A probe MIG is a standalone billable size-1 GPU resource outside
        #    Terraform state, scoped purely to THIS run's id and INDEPENDENT of the
        #    cluster, so it runs on every exit — including a failed destroy — in its
        #    own guarded block.
        probe_reclaim, probe_errors = _reclaim_gpu_probes(project)

        if destroy_error is not None:
            # The cluster destroy FAILED, so the cluster may still be live. Do NOT run
            # the PD backstop now: deleting cluster-labeled disks while the cluster
            # still exists could delete disks still attached to, or protected for, that
            # live cluster. The destroy failure is the primary, UNMASKED error
            # (teardown fails visibly; a rerun recovers and, once the cluster is
            # confirmed gone, reclaims the disks). Attach any probe-reclaim status so a
            # retained billable probe MIG is still reported alongside it.
            result = k8s.error_result(PLATFORM, destroy_error)
            if probe_errors:
                result["cleanup_errors"] = probe_errors
            return k8s.emit(result)

        # 5) Backstop: reclaim any PVC-backed PD whose CSI delete raced teardown. A
        #    `goog-k8s-cluster-name` label match DISCOVERS candidates across every zone a
        #    run-owned pool may have selected (baseline + test GPU pool pick zones
        #    independently), but deletion is AUTHORIZED only for a disk in this run's
        #    full-run ownership ledger (captured from live PVs in step 2 above), never from
        #    the truncated cluster label alone. Reached ONLY after the cluster destroy is
        #    CONFIRMED (terraform_destroy returned no error, which also covers an already
        #    not-found cluster) AND ownership was proven above, so the ledger-owned disks
        #    scanned here are genuinely run-owned AND their cluster is gone. Each disk is
        #    additionally described and required to be detached/deletable before deletion.
        reclaim, pd_errors = _reclaim_orphan_pds(project, cluster_name, capture_fresh=capture_status["complete"])
        # PROPAGATE a capture / ledger-persistence failure. When this teardown's own live-PV
        # capture did not complete, there is no FRESH full-run signal to prove a new
        # out-of-ledger cluster-labeled disk is a prefix-colliding OTHER run's, so
        # delete_orphan_pds (capture_fresh=False) already surfaces every unmatched disk as
        # `unverified`; additionally record the capture failure itself so a lost capture is
        # never hidden behind an otherwise-clean teardown.
        capture_errors: list[str] = []
        if not capture_status["complete"] and not capture_status.get("absent"):
            # A capture that could not COMPLETE against a LIVE cluster is a real anomaly and
            # must surface. But an EXPECTED skip because the cluster was CONFIRMED absent
            # (capture_status["absent"]) is not a failure — the durable ledger + probe
            # backstops still fail closed on any surviving/unverified inventory below, so a
            # legitimately already-gone cluster must not be forced to a false failure here.
            capture_errors.append(
                "run-owned Persistent Disk identity capture did not complete this teardown, so a "
                "disk appearing after any earlier capture cannot be proven foreign; treating "
                f"unmatched cluster-labeled disks as unverified: {capture_status['error']}"
            )
        cleanup_errors = capture_errors + pd_errors + probe_errors

        # A failed capture, a failed disk/probe LISTING, or any surviving resource means
        # run-owned (billable) resources cannot be confirmed reclaimed — never present that as
        # a clean teardown.
        if cleanup_errors:
            result.update(
                {
                    "success": False,
                    "error_type": "cleanup_incomplete",
                    "error": (
                        "[bucket=cleanup_incomplete] GKE cluster destroyed, but run-owned "
                        "billable resource reclaim is UNCONFIRMED: " + "; ".join(cleanup_errors)
                    ),
                    "resources_deleted": resources_deleted,
                    "cleanup_errors": cleanup_errors,
                }
            )
        else:
            # Every run-owned billable resource confirmed reclaimed — drop the
            # retained-probe marker so a later preservation rerun short-circuits.
            k8s.clear_retained_probes_marker()
            if cluster_confirmed_absent:
                message = (
                    f"GKE cluster {cluster_name} was already CONFIRMED absent live; stale tracked "
                    f"state reconciled and {len(reclaim['deleted'])} orphaned PD(s) and "
                    f"{len(probe_reclaim['deleted'])} GPU-preflight probe resource(s) reclaimed."
                )
            else:
                message = (
                    f"GKE cluster {cluster_name} destroyed and confirmed absent; "
                    f"{len(reclaim['deleted'])} orphaned PD(s) and "
                    f"{len(probe_reclaim['deleted'])} GPU-preflight probe resource(s) reclaimed."
                )
            result.update(
                {
                    "success": True,
                    "message": message,
                    "resources_deleted": resources_deleted,
                }
            )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        # PREFLIGHT-ISOLATED CLEANUP: the GPU capacity-preflight probe backstop is
        # scoped purely to THIS run's id and is INDEPENDENT of any cluster / Terraform state,
        # so it MUST run on EVERY non-preservation exit once the project is resolved —
        # including a raise BEFORE the guarded backstop blocks above ever executed (e.g.
        # terraform_init reconciling a stale lock, classify_state, scoped_name, or the
        # destroy-ownership describe raising). Those propagate straight here, and this handler
        # previously exited WITHOUT reclaiming a billable probe MIG/instance-template. Keep the
        # original failure as the PRIMARY reported error and merge any probe-reclaim failure
        # into cleanup_errors; leave the retained-probe ledger intact (do not clear it on a
        # failed exit) so a rerun re-attempts the reclaim while any leak remains.
        result = k8s.error_result(PLATFORM, exc)
        if project is not None:
            _probe_reclaim, probe_errors = _reclaim_gpu_probes(project)
            if probe_errors:
                result["cleanup_errors"] = list(result.get("cleanup_errors", [])) + probe_errors

    return k8s.emit(result)


if __name__ == "__main__":
    sys.exit(main())
