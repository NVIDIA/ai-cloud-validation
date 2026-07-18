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
gated by the unreleased K8sMultiClusterSameVpcCheck) is success. The location /
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
_PLACEHOLDER_LOCATION = "us-central1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Destroy the secondary shared-VPC GKE cluster via Terraform.")
    parser.add_argument(
        "--cluster-name",
        default="isv-gke-secondary",
        help="Secondary cluster name base (RUN_ID-suffixed by the stub).",
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

        if not k8s.state_exists(k8s.SHARED_VPC_TF_DIR, state_file):
            result.update(
                {
                    "success": True,
                    "message": f"Secondary cluster state {state_file} absent - nothing to destroy.",
                    "resources_deleted": [],
                }
            )
            return k8s.emit(result)

        k8s.terraform_init(k8s.SHARED_VPC_TF_DIR)

        # Re-derive the required vars from the SECONDARY's OWN persisted state so
        # the var-less destroy never depends on the primary state (best-effort
        # teardown may already have destroyed it). location has no default, so it
        # must be present; network/subnetwork are the destroy-time fallbacks for
        # the module's try(primary_state, var) wiring.
        def _own(name: str, fallback: str = "") -> str:
            try:
                return k8s.terraform_output_raw(k8s.SHARED_VPC_TF_DIR, state_file, name)
            except k8s.LifecycleError:
                return fallback

        secondary_location = _own("location", _PLACEHOLDER_LOCATION)
        tf_vars = {
            "project": project,
            "cluster_name": secondary_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "location": secondary_location,
            "network": _own("network"),
            "subnetwork": _own("subnetwork"),
        }
        # Fail-closed backstop against a state entry that now resolves to a
        # deleted-and-replaced same-name FOREIGN secondary: re-verify the LIVE
        # ownership marker before destroy and SKIP on a definitive non-ownership
        # signal (present-but-different-run, or absent). A not-found or transiently
        # unreadable marker falls through so a describe flake never leaks our own.
        destroy_ok, ownership_reason = k8s.destroy_ownership_ok(secondary_name, secondary_location, project)
        if not destroy_ok:
            k8s.log(f"warning: skipping secondary cluster destroy — {ownership_reason}")
            result.update(
                {
                    "success": True,
                    "message": f"Secondary cluster destroy skipped — {ownership_reason}.",
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
