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
  3. BACKSTOP: after the cluster is gone, delete Compute disks in ANY zone whose
     goog-k8s-cluster-name label == THIS run's cluster (exact ownership, never a
     name-pattern sweep). A failed disk listing or a surviving disk is surfaced
     as `cleanup_errors` with `success=False` — a leaked billable disk never
     presents as a clean teardown.
  4. BACKSTOP: reclaim any GPU capacity-preflight probe MIG / instance-template
     this run left behind (setup + create_test_gpu_node_pool each probe), scoped
     to THIS run's probe-name prefix. A probe MIG is a standalone billable size-1
     GPU resource outside Terraform state and without the cluster label, so a
     failed inline probe delete would otherwise leak silently; an unconfirmed
     reclaim is surfaced as `cleanup_errors` with `success=False` too. This probe
     backstop is scoped purely to the run id (independent of any cluster state), so
     it runs on EVERY non-preservation path — including when no cluster state
     exists (a setup GPU-preflight bail can leave a probe MIG without ever writing
     cluster state) and after a failed `terraform destroy` (each backstop runs in
     its own guarded block, so a destroy raise no longer skips the reclaim).

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
_PLACEHOLDER = "placeholder"
_PLACEHOLDER_ZONE = "us-central1-a"


def _pd_reclaim_errors(reclaim: dict[str, Any], cluster_name: str) -> list[str]:
    """Cleanup-error strings for any run-owned Persistent Disk that could not be
    confirmed reclaimed (a failed listing or a surviving disk). Empty when the
    backstop confirmed every run-owned disk is gone."""
    errors: list[str] = []
    if reclaim["list_error"]:
        errors.append(
            "could not list run-owned Persistent Disks (label "
            f"goog-k8s-cluster-name={cluster_name}); orphaned disks may remain: {reclaim['list_error']}"
        )
    if reclaim["failed"]:
        errors.append(f"failed to delete run-owned Persistent Disk(s): {', '.join(reclaim['failed'])}")
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


def _reclaim_orphan_pds(project: str, cluster_name: str) -> tuple[dict[str, Any], list[str]]:
    """Run the run-scoped orphan-PD backstop in its own guarded block so it never
    raises past teardown (an unexpected failure becomes a cleanup-error string
    instead of masking the primary result)."""
    try:
        reclaim = k8s.delete_orphan_pds(project, cluster_name)
    except BaseException as exc:  # a backstop must never crash the teardown
        reclaim = {"deleted": [], "failed": [], "list_error": f"disk reclaim raised: {exc}"}
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Destroy the primary GKE cluster via Terraform.")
    parser.add_argument("--cluster-name", default="isv-gke", help="Cluster name base (RUN_ID-suffixed by the stub).")
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        help="Preserve the cluster for debugging (GCP_K8S_SKIP_TEARDOWN=true).",
    )
    args = parser.parse_args()

    if args.skip_destroy:
        return _emit_preserved_teardown()

    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        project = k8s.resolve_project_id()
        cluster_name = k8s.scoped_name(args.cluster_name)
        state_file = k8s.cluster_state_file()

        if not k8s.state_exists(k8s.CLUSTER_TF_DIR, state_file):
            # No cluster Terraform state: setup bailed before `terraform apply`
            # wrote it (e.g. the GPU capacity preflight raised in select_gpu_zone).
            # The cluster and its PVC-backed disks never existed, but a standalone
            # size-1 GPU capacity-probe MIG can still be orphaned — its backstop is
            # scoped purely to THIS run's id, independent of cluster state, so it
            # MUST run here too. A retained billable probe MIG can never present as
            # a clean "nothing to destroy".
            probe_reclaim, cleanup_errors = _reclaim_gpu_probes(project)
            if cleanup_errors:
                result.update(
                    {
                        "success": False,
                        "error_type": "cleanup_incomplete",
                        "error": (
                            "[bucket=cleanup_incomplete] No cluster state to destroy, but "
                            "run-owned GPU capacity-preflight probe reclaim is UNCONFIRMED: "
                            + "; ".join(cleanup_errors)
                        ),
                        "resources_deleted": [],
                        "cleanup_errors": cleanup_errors,
                    }
                )
            else:
                result.update(
                    {
                        "success": True,
                        "message": (
                            f"Cluster state {state_file} absent - nothing to destroy; "
                            f"{len(probe_reclaim['deleted'])} orphaned GPU-preflight probe "
                            "resource(s) reclaimed."
                        ),
                        "resources_deleted": [],
                    }
                )
            return k8s.emit(result)

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
        #    It returns ok=False on EVERY unproven outcome: a marker
        #    present-but-a-different-run, a marker absent on a live cluster, OR a
        #    marker that is UNREADABLE (auth / permission / transport / malformed
        #    describe). A describe flake therefore NEVER authorizes destroying a
        #    same-name cluster we cannot prove we own — teardown fails visibly and a
        #    rerun with a readable marker recovers.
        destroy_ok, ownership_reason = k8s.destroy_ownership_ok(cluster_name, location, project)

        if not destroy_ok:
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

        # We own the cluster (live marker matched, or a describe flake / not-found
        # fell through).
        # 2) Reclaim run PVCs while the cluster still lives (best-effort).
        try:
            k8s.install_kubeconfig(cluster_name, location, project)
            k8s.reclaim_run_pvcs()
        except k8s.LifecycleError as exc:
            k8s.log(f"warning: PVC reclaim skipped (cluster may be unreachable): {exc.detail}")

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
            resources_deleted = ["google_container_cluster", "google_container_node_pool"]
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

        # 5) Backstop: reclaim any PVC-backed PD whose CSI delete raced teardown, by
        #    THIS run's cluster label across every zone a run-owned pool may have
        #    selected (baseline + test GPU pool pick zones independently). Reached
        #    ONLY after the cluster destroy is CONFIRMED (terraform_destroy returned no
        #    error, which also covers an already not-found cluster) AND ownership was
        #    proven above, so the cluster-labeled disks scanned here are genuinely
        #    run-owned AND their cluster is gone. Each disk is additionally described
        #    and required to be detached/deletable before deletion.
        reclaim, pd_errors = _reclaim_orphan_pds(project, cluster_name)
        cleanup_errors = pd_errors + probe_errors

        # A failed disk/probe LISTING or any surviving resource means run-owned
        # (billable) resources cannot be confirmed reclaimed — never present that as
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
            result.update(
                {
                    "success": True,
                    "message": (
                        f"GKE cluster {cluster_name} destroyed; {len(reclaim['deleted'])} orphaned PD(s) "
                        f"and {len(probe_reclaim['deleted'])} GPU-preflight probe resource(s) reclaimed."
                    ),
                    "resources_deleted": resources_deleted,
                }
            )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc)

    return k8s.emit(result)


if __name__ == "__main__":
    sys.exit(main())
