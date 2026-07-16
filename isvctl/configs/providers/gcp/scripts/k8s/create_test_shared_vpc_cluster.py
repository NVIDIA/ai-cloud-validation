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

"""Create a SECONDARY GKE cluster in the primary cluster's VPC (setup phase).

Proves multiple clusters coexist in ONE VPC network natively on GKE (the GCP
analog of the EKS oracle's create_shared_vpc_cluster.sh). The secondary attaches
to the SAME network/subnetwork as the primary (read from the primary state via
terraform_remote_state), waits for >=1 Ready node using an ISOLATED kubeconfig
(so the ambient context stays on the primary), then emits the `multi_cluster`
payload K8sMultiClusterSameVpcCheck consumes.

Gated by requires_available_validations: [K8sMultiClusterSameVpcCheck] — the
check is UNRELEASED, so this step only runs under ISVTEST_INCLUDE_UNRELEASED=1.

The GKE up-state 'RUNNING' is mapped to the contract sentinel 'ACTIVE' (the
check exact-matches 'ACTIVE'); the raw GKE value is never passed through.

AWS reference: ../../aws/scripts/eks/create_shared_vpc_cluster.sh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_lib as k8s

PLATFORM = "kubernetes"

_APPLY_TIMEOUT = 1500
_READY_TIMEOUT = 900


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision a secondary GKE cluster sharing the primary VPC.")
    parser.add_argument(
        "--cluster-name",
        default="isv-gke-secondary",
        help="Secondary cluster name base (RUN_ID-suffixed by the stub).",
    )
    parser.add_argument("--location", required=True, help="Location for the secondary cluster (match the primary).")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": PLATFORM}
    try:
        project = k8s.resolve_project_id()
        secondary_name = k8s.scoped_name(args.cluster_name)
        state_file = k8s.shared_vpc_state_file()

        # Primary identity + shared network/subnetwork read live from the primary
        # cluster's Terraform state at create.
        primary_name = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "cluster_name")
        primary_location = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "location")
        network_id = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "network")
        subnetwork_id = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "subnetwork")

        k8s.terraform_init(k8s.SHARED_VPC_TF_DIR)
        tf_vars = {
            "project": project,
            "cluster_name": secondary_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "location": args.location,
            # Persist the shared network/subnetwork into the secondary's OWN state
            # (fallback vars) so a var-less destroy resolves after the primary
            # state's outputs are gone; at create the live primary output wins.
            "network": network_id,
            "subnetwork": subnetwork_id,
            # Pin the secondary's node pool to ONE zone (node_count is PER-ZONE) so a
            # regional secondary does not multiply node_count across region zones.
            "node_locations": [k8s.zone_for_location(args.location)],
        }
        k8s.terraform_apply(k8s.SHARED_VPC_TF_DIR, state_file, tf_vars, timeout=_APPLY_TIMEOUT)

        # Wait for the secondary to report a Ready node (isolated kubeconfig).
        secondary_ready_nodes = k8s.wait_secondary_ready(secondary_name, args.location, project, timeout=_READY_TIMEOUT)

        # Map the GKE RUNNING up-state to the contract sentinel 'ACTIVE'.
        primary_status = k8s.gke_cluster_status_active(primary_name, primary_location, project)
        secondary_status = k8s.gke_cluster_status_active(secondary_name, args.location, project)

        result.update(
            {
                "success": True,
                "test_id": "K8S26-01",
                "tenancy_id": project,
                "network_id": network_id,
                "clusters": [
                    {
                        "name": primary_name,
                        "role": "primary",
                        "tenancy_id": project,
                        "network_id": network_id,
                        "status": primary_status,
                    },
                    {
                        "name": secondary_name,
                        "role": "secondary",
                        "tenancy_id": project,
                        "network_id": network_id,
                        "status": secondary_status,
                        "ready_node_count": secondary_ready_nodes,
                    },
                ],
            }
        )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc)

    return k8s.emit(result)


if __name__ == "__main__":
    sys.exit(main())
