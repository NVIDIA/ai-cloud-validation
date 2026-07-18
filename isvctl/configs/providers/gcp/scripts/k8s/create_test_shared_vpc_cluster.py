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
terraform_remote_state). After apply, both clusters are DESCRIBED live through
the GKE API and their shared network/subnetwork membership + active state are
verified BEFORE the secondary kubeconfig is installed and its node readiness is
waited on — control-plane membership is the prerequisite for interpreting
secondary-node readiness. Each cluster's OBSERVED network is then emitted in the
`multi_cluster` payload K8sMultiClusterSameVpcCheck consumes (never the primary's
state value echoed to both).

Gated by requires_available_validations: [K8sMultiClusterSameVpcCheck], so this
step runs whenever the released check is selected.

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

        # The harness preserves run-scoped resources (GCP_K8S_SKIP_TEARDOWN), so the
        # secondary may already exist from an earlier per-step worker in the run
        # while THIS worktree's local state is empty; a blind create would collide
        # on 409 "already exists". Detect that and ADOPT it (import) instead.
        secondary_in_state = k8s.terraform_state_has(
            k8s.SHARED_VPC_TF_DIR, state_file, "google_container_cluster.secondary"
        )
        secondary_exists = secondary_in_state or k8s.gke_cluster_exists(secondary_name, args.location, project)

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
        if secondary_exists:
            # ADOPT the preserved secondary cluster AND its node pool. The harness
            # keeps every run resource alive (GCP_K8S_SKIP_TEARDOWN), so a fresh
            # worktree's empty local state — or a PARTIAL prior import that captured
            # only the cluster — makes a normal apply try to re-CREATE the already-
            # live cluster/pool and collide on 409 "already exists". Import each
            # managed resource the cloud has but local state lacks, then refresh-only
            # (never a normal apply — an import + apply would REPLACE the cluster over
            # its API-reported initial_node_count). Readiness is verified live via
            # wait_secondary_ready below.
            secondary_id = f"projects/{project}/locations/{args.location}/clusters/{secondary_name}"
            if not secondary_in_state:
                # FAIL CLOSED before importing a secondary this worktree's state does
                # not track: require the full-run-identity ownership marker so a
                # stale/colliding same-name cluster is never adopted (and later
                # destroyed) as though this run owned it. Its shared-VPC membership
                # is additionally verified live below before any success is emitted.
                k8s.verify_cluster_ownership(secondary_name, args.location, project)
                k8s.terraform_import(
                    k8s.SHARED_VPC_TF_DIR, state_file, "google_container_cluster.secondary", secondary_id, tf_vars
                )
            # The node pool is declared INSIDE this module, so importing the cluster
            # alone leaves it untracked and a later apply tries to CREATE the already-
            # live pool (the observed 409). Import it when the cloud has it but local
            # state lacks it, by its terraform-derived name.
            pool_address = "google_container_node_pool.secondary"
            pool_name = k8s.secondary_pool_name(secondary_name)
            if not k8s.terraform_state_has(
                k8s.SHARED_VPC_TF_DIR, state_file, pool_address
            ) and k8s.gke_node_pool_exists(secondary_name, pool_name, args.location, project):
                pool_id = f"{secondary_id}/nodePools/{pool_name}"
                k8s.terraform_import(k8s.SHARED_VPC_TF_DIR, state_file, pool_address, pool_id, tf_vars)
            k8s.terraform_refresh_only(k8s.SHARED_VPC_TF_DIR, state_file, tf_vars)
        else:
            k8s.terraform_apply(k8s.SHARED_VPC_TF_DIR, state_file, tf_vars, timeout=_APPLY_TIMEOUT)

        # Stamp (or confirm) the cloud-side ownership marker carrying the FULL run
        # identity on the secondary, so a later cross-worker adopt can prove it owns
        # this cluster before importing it. Idempotent (no-op when already present).
        k8s.ensure_cluster_ownership_label(secondary_name, args.location, project)

        # Describe BOTH clusters LIVE and verify shared membership + active state
        # BEFORE installing/waiting on the secondary kubeconfig. The GKE up-state
        # 'RUNNING' is mapped to the contract sentinel 'ACTIVE' by
        # read_cluster_membership. A refresh-only ADOPTED secondary is never
        # trusted from Terraform state alone — its live network/subnetwork must
        # actually match the primary's, or a preserved same-name cluster attached
        # to a DIFFERENT VPC could be reported as sharing the primary's network.
        primary_net, primary_subnet, primary_status = k8s.read_cluster_membership(
            primary_name, primary_location, project
        )
        secondary_net, secondary_subnet, secondary_status = k8s.read_cluster_membership(
            secondary_name, args.location, project
        )
        if primary_status != "ACTIVE":
            raise k8s.LifecycleError(
                "transient",
                f"[bucket=transient] primary cluster {primary_name} is not active "
                f"(live status={primary_status}); cannot assert shared-VPC coexistence.",
            )
        if secondary_status != "ACTIVE":
            raise k8s.LifecycleError(
                "transient",
                f"[bucket=transient] secondary cluster {secondary_name} is not active "
                f"(live status={secondary_status}); cannot assert shared-VPC coexistence.",
            )
        if not k8s.same_network(primary_net, secondary_net):
            raise k8s.LifecycleError(
                "config_error",
                f"[bucket=config_error] secondary cluster {secondary_name} attached to "
                f"network '{secondary_net}', not the primary's shared VPC '{primary_net}'. "
                "Refusing to report same-VPC coexistence for clusters on different networks.",
            )
        if not k8s.same_network(primary_subnet, secondary_subnet):
            raise k8s.LifecycleError(
                "config_error",
                f"[bucket=config_error] secondary cluster {secondary_name} subnetwork "
                f"'{secondary_subnet}' differs from the primary's '{primary_subnet}'.",
            )

        # Membership proven — NOW wait for the secondary to report a Ready node
        # (isolated kubeconfig; the ambient context stays on the primary).
        secondary_ready_nodes = k8s.wait_secondary_ready(secondary_name, args.location, project, timeout=_READY_TIMEOUT)

        result.update(
            {
                "success": True,
                "test_id": "K8S26-01",
                "tenancy_id": project,
                # OBSERVED shared network (verified equal for both clusters).
                "network_id": primary_net,
                "clusters": [
                    {
                        "name": primary_name,
                        "role": "primary",
                        "tenancy_id": project,
                        "network_id": primary_net,
                        "status": primary_status,
                    },
                    {
                        "name": secondary_name,
                        "role": "secondary",
                        "tenancy_id": project,
                        # The secondary's OWN observed network (proven to match).
                        "network_id": secondary_net,
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
