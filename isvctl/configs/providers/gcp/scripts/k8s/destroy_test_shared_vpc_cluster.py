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

"""Destroy the secondary shared-VPC GKE cluster (teardown phase).

Runs BEFORE the primary teardown so the secondary releases the shared network
first. Exact-ownership cleanup via the threaded secondary-cluster state.
Idempotent: an already-absent secondary (no state / never ran — the step is
gated by the released K8sMultiClusterSameVpcCheck) is success. The location /
network / subnetwork vars are re-derived from the SECONDARY's OWN persisted state
(not the primary's) so the var-less destroy resolves even after a best-effort
teardown already destroyed the primary — no "No value for required variable" and
no dependency on the primary state's outputs surviving.

Honors --skip-destroy (GCP_K8S_SKIP_TEARDOWN=true): a preservation request
short-circuits to a structured success BEFORE any auth/Terraform, so the
secondary cluster is preserved alongside the primary.

AWS reference: ../../aws/scripts/eks/destroy_shared_vpc_cluster.sh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_lib as k8s

PLATFORM = "kubernetes"
_DESTROY_TIMEOUT = 1500
# Absence-confirmation budget for the ambiguous-create reconcile path. Passed
# EXPLICITLY (not left at reconcile_orphaned_cluster's 1800 default) so the
# longest single internal wait stays at the 1500s destroy budget, strictly below
# this step's 1800s orchestrator cap in config/k8s.yaml. Leaving the 1800 default
# made the absence wait EQUAL the step cap, so the orchestrator could kill the
# step at the same instant the wait would raise its structured cleanup_incomplete
# failure — swallowing the diagnostic. 1500s remains a generous confirmation
# budget: the preceding terraform_destroy already blocks on the delete operation,
# so the poll normally confirms absence on its first iteration.
_RECONCILE_WAIT_TIMEOUT = 1500
_SECONDARY_ADDRESS = "google_container_cluster.secondary"


def _reconcile_stateless_secondary(project: str, secondary_name: str, location: str, state_file: str) -> int:
    """Reconcile the EXACT secondary cluster live when its local state is absent or
    VALID-EMPTY.

    File presence alone cannot distinguish "never created" from "already destroyed",
    and a failed create apply can leave the exact secondary present before its state
    address is durable. Report idempotent success ONLY after confirmed cloud absence:
    describe the exact deterministic secondary in the known (deterministic)
    project/location, treat a confirmed-absent cluster as clean, import + destroy +
    wait a run-owned leak, and fail visibly on unreadable/mismatched ownership."""
    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        tf_vars = {
            "project": project,
            "cluster_name": secondary_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "location": location,
            "network": "",
            "subnetwork": "",
        }
        outcome = k8s.reconcile_orphaned_cluster(
            k8s.SHARED_VPC_TF_DIR,
            state_file,
            _SECONDARY_ADDRESS,
            secondary_name,
            location,
            project,
            tf_vars,
            destroy_timeout=_DESTROY_TIMEOUT,
            wait_timeout=_RECONCILE_WAIT_TIMEOUT,
        )
        if outcome == "reclaimed":
            result.update(
                {
                    "success": True,
                    "message": (
                        f"Secondary cluster {secondary_name} had no durable local state but was found "
                        "live and run-owned (an ambiguous create); imported, destroyed, and confirmed "
                        "absent."
                    ),
                    "resources_deleted": ["google_container_cluster"],
                }
            )
        else:  # "absent"
            result.update(
                {
                    "success": True,
                    "message": (
                        f"Secondary cluster state {state_file} is absent/valid-empty and the cluster "
                        "is confirmed absent live - nothing to destroy."
                    ),
                    "resources_deleted": [],
                }
            )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc)
    return k8s.emit(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Destroy the secondary shared-VPC GKE cluster via Terraform.")
    parser.add_argument(
        "--cluster-name",
        default="isv-gke-secondary",
        help="Secondary cluster name base (RUN_ID-suffixed by the stub).",
    )
    parser.add_argument(
        "--location",
        required=True,
        help="Secondary cluster location (deterministic; used to describe the exact "
        "cluster for the absent/valid-empty-state live reconcile).",
    )
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        help="Preserve the secondary cluster for debugging (GCP_K8S_SKIP_TEARDOWN=true).",
    )
    args = parser.parse_args()

    if args.skip_destroy:
        # Preservation short-circuit BEFORE any auth/Terraform: keep the secondary
        # in lockstep with the preserved primary instead of tearing it down here.
        return k8s.emit(
            {
                "success": True,
                "platform": PLATFORM,
                "skipped": True,
                "message": "Secondary-cluster destroy skipped (GCP_K8S_SKIP_TEARDOWN=true); secondary cluster preserved.",
            }
        )

    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        project = k8s.resolve_project_id()
        secondary_name = k8s.scoped_name(args.cluster_name)
        state_file = k8s.shared_vpc_state_file()

        # Initialize unconditionally (idempotent) so `terraform state list`
        # classification reads a ready local backend, then classify by the EXACT
        # secondary address rather than mere file presence: an absent OR valid-empty
        # state must be reconciled live (a failed create apply can leave the exact
        # secondary present before its state address is durable), and a failed state
        # read is ownership_unprovable — never a clean "nothing to destroy".
        k8s.terraform_init(k8s.SHARED_VPC_TF_DIR)
        state_class = k8s.classify_state(k8s.SHARED_VPC_TF_DIR, state_file, _SECONDARY_ADDRESS)

        if state_class == "unreadable":
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_unprovable",
                    "error": (
                        f"[bucket=ownership_unprovable] refusing to report secondary cluster "
                        f"{secondary_name} clean: its Terraform state {state_file} exists but "
                        "`terraform state list` could not be read, so its provenance is unprovable. "
                        "A rerun with readable state recovers."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)

        if state_class in ("absent", "empty"):
            # No tracked secondary address: reconcile the EXACT deterministic secondary
            # live and report success only after confirmed cloud absence.
            return _reconcile_stateless_secondary(project, secondary_name, args.location, state_file)

        # state_class == "tracked": the secondary address is in state.
        # Re-derive the required vars from the SECONDARY's OWN persisted state so
        # the var-less destroy never depends on the primary state (best-effort
        # teardown may already have destroyed it). network/subnetwork are the
        # destroy-time fallbacks for the module's try(primary_state, var) wiring, so
        # an empty fallback for THEM is harmless (they never gate ownership).
        def _own(name: str, fallback: str = "") -> str:
            try:
                return k8s.terraform_output_raw(k8s.SHARED_VPC_TF_DIR, state_file, name)
            except k8s.LifecycleError:
                return fallback

        # location has NO placeholder fallback — it is REQUIRED to describe the LIVE
        # secondary and prove ownership before destroy. A wrong (placeholder) location
        # would describe a DIFFERENT resource and read a false not_found, authorizing
        # destruction of a cluster whose ownership was never proven. If it is
        # unreadable, fail CLOSED with a visible structured failure.
        try:
            secondary_location = k8s.terraform_output_raw(k8s.SHARED_VPC_TF_DIR, state_file, "location")
        except k8s.LifecycleError as exc:
            k8s.log(f"warning: refusing secondary cluster destroy — location unreadable from state: {exc.detail}")
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_unprovable",
                    "error": (
                        f"[bucket=ownership_unprovable] refusing to destroy secondary cluster "
                        f"{secondary_name}: its create-time location is unreadable from state "
                        f"({exc.detail}), so run ownership cannot be verified. The cluster was left "
                        "untouched; a rerun with readable state recovers."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)

        tf_vars = {
            "project": project,
            "cluster_name": secondary_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "location": secondary_location,
            "network": _own("network"),
            "subnetwork": _own("subnetwork"),
        }
        # Fail-closed gate against a state entry that now resolves to a
        # deleted-and-replaced same-name FOREIGN secondary: re-verify the LIVE
        # ownership marker before destroy and REFUSE unless ownership is positively
        # proven (marker matches this run, or a clean not_found). A
        # present-but-different-run marker, an absent marker, OR an UNREADABLE marker
        # all fail closed as a VISIBLE failure — preserving a foreign or
        # ownership-unprovable same-name secondary must never present as a clean skip.
        destroy_ok, ownership_reason = k8s.destroy_ownership_ok(secondary_name, secondary_location, project)
        if not destroy_ok:
            k8s.log(f"warning: refusing secondary cluster destroy — {ownership_reason}")
            result.update(
                {
                    "success": False,
                    "error_type": "ownership_conflict",
                    "error": (
                        f"[bucket=ownership_conflict] refusing to destroy secondary cluster "
                        f"{secondary_name}: {ownership_reason}. The cluster was left untouched."
                    ),
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)
        k8s.terraform_destroy(k8s.SHARED_VPC_TF_DIR, state_file, tf_vars, timeout=_DESTROY_TIMEOUT)

        result.update(
            {
                "success": True,
                "message": f"Secondary cluster {secondary_name} destroyed.",
                "resources_deleted": ["google_container_cluster"],
            }
        )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc)

    return k8s.emit(result)


if __name__ == "__main__":
    sys.exit(main())
