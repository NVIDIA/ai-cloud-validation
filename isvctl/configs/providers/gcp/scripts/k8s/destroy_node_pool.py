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

        if not k8s.state_exists(k8s.NODE_POOL_TF_DIR, state_file):
            result.update(
                {
                    "success": True,
                    "message": f"Node pool state {state_file} absent - nothing to destroy.",
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)

        k8s.terraform_init(k8s.NODE_POOL_TF_DIR)

        # Recover the cluster wiring this pool PERSISTED in its own state at create
        # so the var-less destroy resolves without the primary state's outputs
        # (best-effort teardown may already have destroyed the primary before this
        # retry). The pool is targeted by its own state resource id, so an empty
        # fallback is harmless — the value only needs to be present, never live.
        try:
            cluster_name = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "cluster_name")
        except k8s.LifecycleError:
            cluster_name = ""
        try:
            cluster_location = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "cluster_location")
        except k8s.LifecycleError:
            cluster_location = ""

        tf_vars = {
            "project": project,
            "pool_name": pool_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "cluster_name": cluster_name,
            "cluster_location": cluster_location,
            "machine_type": _PLACEHOLDER_MACHINE_TYPE,
        }
        # Fail-closed backstop before destroying a state-targeted node pool: when the
        # parent cluster wiring is recoverable, re-verify the PARENT cluster's live
        # ownership marker and SKIP the pool destroy on a definitive non-ownership
        # signal (present-but-different-run, or absent) so a pool on a
        # deleted-and-replaced same-name FOREIGN cluster is never destroyed. An
        # unrecoverable parent (primary state already gone) or a transiently-unreadable
        # marker falls through to the existing state-targeted destroy.
        if cluster_name and cluster_location:
            destroy_ok, ownership_reason = k8s.destroy_ownership_ok(cluster_name, cluster_location, project)
            if not destroy_ok:
                k8s.log(f"warning: skipping node pool destroy — parent cluster {ownership_reason}")
                result.update(
                    {
                        "success": True,
                        "message": f"Node pool {pool_name} destroy skipped — parent cluster {ownership_reason}.",
                        "resources_deleted": [],
                    }
                )
                return k8s.emit(result)
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
