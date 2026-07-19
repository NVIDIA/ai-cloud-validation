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

"""Destroy a GKE test node pool created by create_node_pool.py (teardown phase).

Serves both destroy_test_node_pool and destroy_test_gpu_node_pool. Exact-
ownership cleanup via the threaded state file derived from the RUN_ID-scoped pool
name — never a broad label/name discovery sweep. Idempotent: an already-absent
pool (no state file) is success, not an error.

A var-less `terraform destroy` re-evaluates the whole config, so it needs a value
for every no-default variable. Target-identifying inputs (project, pool_name,
cluster_state_path) are re-derived to the SAME run-scoped values create used;
inputs the delete does not consume (machine_type) take a benign placeholder. The
cluster wiring the module reads via terraform_remote_state at create is PERSISTED
in this pool's own state (cluster_name / cluster_location outputs), read back
here, and threaded as fallback vars — so the destroy still resolves even if a
best-effort teardown already destroyed the primary before a transient retry.

Honors --skip-destroy (GCP_K8S_SKIP_TEARDOWN=true): a preservation request
short-circuits to a structured success BEFORE any auth/Terraform, so the node
pools are preserved alongside the cluster instead of being torn down while the
primary teardown is skipped.

AWS reference: ../../aws/scripts/eks/destroy_node_pool.sh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_lib as k8s

PLATFORM = "kubernetes"
_DESTROY_TIMEOUT = 1200
# Benign placeholder for the delete-irrelevant machine_type var (terraform
# destroy targets by state, not by this value).
_PLACEHOLDER_MACHINE_TYPE = "e2-standard-4"
_POOL_ADDRESS = "google_container_node_pool.this"


def _resolve_parent_for_reconcile() -> str | tuple[str, str]:
    """Resolve the parent cluster ``(name, location)`` for the empty/absent pool-state
    reconcile from the PRIMARY cluster state.

    A pool whose local state is absent or VALID-EMPTY no longer carries its own
    ``cluster_name`` / ``cluster_location`` outputs, so the parent identity needed to
    describe the exact pool live is recovered from the primary cluster state (the same
    source create_node_pool reads via terraform_remote_state). Returns:

      * ``'absent'``     - the primary cluster state is absent OR valid-empty (the
                           parent never existed / was already destroyed, so the pool
                           cannot exist) -> the caller reports idempotent clean;
      * ``'unreadable'`` - the primary state is present but its address/identity cannot
                           be read -> fail closed (ownership_unprovable);
      * ``(name, loc)``  - the parent identity read cleanly.
    """
    k8s.terraform_init(k8s.CLUSTER_TF_DIR)
    primary_class = k8s.classify_state(
        k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "google_container_cluster.primary"
    )
    if primary_class in ("absent", "empty"):
        return "absent"
    if primary_class == "unreadable":
        return "unreadable"
    try:
        cluster_name = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "cluster_name")
        cluster_location = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "location")
    except k8s.LifecycleError:
        return "unreadable"
    return (cluster_name, cluster_location)


def _reconcile_stateless_pool(project: str, pool_name: str, state_file: str) -> int:
    """Reconcile the EXACT pool live when its local state is absent or VALID-EMPTY.

    File presence alone cannot distinguish "never created" from "already destroyed"
    (the delete-test pool's normal test-phase destroy leaves a valid-empty state its
    teardown safety net then re-enters), so report idempotent success ONLY after
    confirmed cloud absence: describe the exact deterministic pool under its run-owned
    parent, treat a confirmed-absent pool as clean, import + destroy + wait a run-owned
    leak, and fail visibly on an unreadable/mismatched parent identity — never a false
    clean."""
    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        parent = _resolve_parent_for_reconcile()
        if parent == "absent":
            result.update(
                {
                    "success": True,
                    "message": (
                        f"Node pool {pool_name} state {state_file} is absent/valid-empty and its "
                        "parent cluster state is absent/valid-empty (the parent never existed or was "
                        "already destroyed, so the pool cannot exist) - nothing to destroy."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)
        if parent == "unreadable":
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_unprovable",
                    "error": (
                        f"[bucket=ownership_unprovable] refusing to report node pool {pool_name} "
                        "clean: its local state is absent/valid-empty and the primary cluster state "
                        "needed to identify its parent for a live reconcile is unreadable. A rerun "
                        "with readable state recovers."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)
        cluster_name, cluster_location = parent
        tf_vars = {
            "project": project,
            "pool_name": pool_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "cluster_name": cluster_name,
            "cluster_location": cluster_location,
            "machine_type": _PLACEHOLDER_MACHINE_TYPE,
        }
        outcome = k8s.reconcile_orphaned_node_pool(
            k8s.NODE_POOL_TF_DIR,
            state_file,
            _POOL_ADDRESS,
            cluster_name,
            cluster_location,
            pool_name,
            project,
            tf_vars,
            destroy_timeout=_DESTROY_TIMEOUT,
        )
        if outcome == "reclaimed":
            result.update(
                {
                    "success": True,
                    "message": (
                        f"Node pool {pool_name} had no durable local state but was found live under "
                        "its run-owned parent (an ambiguous create); imported, destroyed, and "
                        "confirmed absent."
                    ),
                    "resources_deleted": ["google_container_node_pool"],
                }
            )
        else:  # "absent"
            result.update(
                {
                    "success": True,
                    "message": (
                        f"Node pool {pool_name} state {state_file} is absent/valid-empty and the pool "
                        "is confirmed absent live - nothing to destroy."
                    ),
                    "resources_deleted": [],
                }
            )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc)
    return k8s.emit(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Destroy a GKE test node pool via Terraform.")
    parser.add_argument("--pool-name", required=True, help="Node-pool name base (RUN_ID-suffixed by the stub).")
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        help="Preserve the node pool for debugging (GCP_K8S_SKIP_TEARDOWN=true).",
    )
    args = parser.parse_args()

    if args.skip_destroy:
        # Preservation short-circuit BEFORE any auth/Terraform: an operator asking
        # to keep the cluster keeps its node pools too (the primary teardown skips
        # in lockstep), instead of this dependent step tearing the pool down anyway.
        return k8s.emit(
            {
                "success": True,
                "platform": PLATFORM,
                "skipped": True,
                "message": "Node-pool destroy skipped (GCP_K8S_SKIP_TEARDOWN=true); node pool preserved.",
            }
        )

    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        project = k8s.resolve_project_id()
        pool_name = k8s.scoped_name(args.pool_name)
        state_file = k8s.state_file_for_pool(pool_name)

        # Initialize unconditionally (idempotent) so `terraform state list`
        # classification reads a ready local backend, then classify the pool's state
        # by its EXACT address rather than mere file presence. The delete-test pool's
        # normal test-phase destroy leaves a VALID-EMPTY state its teardown safety net
        # re-enters; file presence alone would then read its outputs, fail, and
        # mis-report a canonical successful cleanup as ownership_unprovable.
        k8s.terraform_init(k8s.NODE_POOL_TF_DIR)
        state_class = k8s.classify_state(k8s.NODE_POOL_TF_DIR, state_file, _POOL_ADDRESS)

        if state_class == "unreadable":
            # `terraform state list` failed: the pool's ownership/identity cannot be
            # classified. Fail CLOSED — a failed state read is ownership_unprovable,
            # never a clean "nothing to destroy".
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_unprovable",
                    "error": (
                        f"[bucket=ownership_unprovable] refusing to report node pool {pool_name} "
                        f"clean: its Terraform state {state_file} exists but `terraform state list` "
                        "could not be read, so its provenance is unprovable. A rerun with readable "
                        "state recovers."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)

        if state_class in ("absent", "empty"):
            # No tracked pool address: the pool was either never durably created (a
            # failed create apply — an ambiguous create) OR already destroyed in the
            # test phase (a valid-empty state). Reconcile the EXACT pool live and
            # report success only after confirmed cloud absence.
            return _reconcile_stateless_pool(project, pool_name, state_file)

        # state_class == "tracked": the pool address is in state.
        # Recover the parent cluster wiring this pool PERSISTED in its OWN state at
        # create (never the primary's), so the parent-ownership gate below can
        # re-verify the LIVE marker before the state-targeted destroy. These outputs
        # are stamped into this pool's own state at create, so a read failure here
        # means the pool's provenance is UNREADABLE — we cannot prove the parent
        # cluster belongs to this run, and must fail CLOSED rather than destroy a pool
        # on a possibly-foreign cluster. An empty fallback would silently SKIP the
        # ownership gate and still destroy, so it is never used.
        try:
            cluster_name = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "cluster_name")
            cluster_location = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "cluster_location")
        except k8s.LifecycleError as exc:
            k8s.log(f"warning: refusing node pool destroy — parent identity unreadable from state: {exc.detail}")
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_unprovable",
                    "error": (
                        f"[bucket=ownership_unprovable] refusing to destroy node pool {pool_name}: its "
                        f"parent cluster identity/location is unreadable from state ({exc.detail}), so "
                        "parent ownership cannot be verified. The pool was left untouched; a rerun with "
                        "readable state recovers."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)

        # Fail-closed parent-ownership gate before the state-targeted destroy: verify
        # the PARENT cluster's LIVE ownership marker and REFUSE the pool destroy unless
        # ownership is positively proven (marker matches this run, or the parent is a
        # clean not_found). A present-but-different-run marker, an absent marker, OR an
        # UNREADABLE marker all fail closed as a VISIBLE failure so a pool on a
        # deleted-and-replaced same-name FOREIGN cluster — or a pool whose parent
        # ownership cannot be read — is never destroyed.
        destroy_ok, ownership_reason = k8s.destroy_ownership_ok(cluster_name, cluster_location, project)
        if not destroy_ok:
            k8s.log(f"warning: refusing node pool destroy — parent cluster {ownership_reason}")
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_conflict",
                    "error": (
                        f"[bucket=ownership_conflict] refusing to destroy node pool {pool_name}: "
                        f"its parent cluster {ownership_reason}. The pool was left untouched."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)

        tf_vars = {
            "project": project,
            "pool_name": pool_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "cluster_name": cluster_name,
            "cluster_location": cluster_location,
            "machine_type": _PLACEHOLDER_MACHINE_TYPE,
        }
        k8s.terraform_destroy(k8s.NODE_POOL_TF_DIR, state_file, tf_vars, timeout=_DESTROY_TIMEOUT)

        result.update(
            {
                "success": True,
                "message": f"Node pool {pool_name} destroyed.",
                "resources_deleted": ["google_container_node_pool"],
            }
        )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc)

    return k8s.emit(result)


if __name__ == "__main__":
    sys.exit(main())
