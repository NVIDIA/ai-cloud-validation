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

"""Create / scale a GKE test node pool via Terraform (GCP analog of the EKS
oracle's create_node_pool.sh).

One script serves three suite steps because `terraform apply` is idempotent:
  * create_test_node_pool      (CPU pool, node_type=cpu)
  * create_test_gpu_node_pool  (GPU pool, node_type=gpu -> guest_accelerator)
  * update_test_node_pool      (re-apply the SAME CPU pool with a higher count)

The pool's local tfstate filename is DERIVED from its RUN_ID-scoped pool name, so
create / scale / destroy of one pool all thread the SAME state without an extra
env var (update re-applies in place; destroy re-derives the same file). Cluster
wiring (name, location) is read from the primary cluster's state via
terraform_remote_state.

Before ANY pool lookup, refresh, import, or apply, the parent cluster is proven to
carry THIS run's exact full-run-identity ownership marker (verify_cluster_ownership)
— on every path (fresh create, in-state scale, adopt) — so a stale pool state can
never create/scale/reconcile a pool inside a colliding same-name foreign cluster.

Every path ends on a POOL-SCOPED completion gate so success means the pool actually
converged, not just that Terraform/import returned:
  * GPU pool: wait for THIS pool's own nodes (its label_selector) to be Ready with
    allocatable nvidia.com/gpu, then apply and read back nvidia.com/gpu.present=true
    on exactly those nodes so the released GPU checks discover them.
  * CPU pool: require a healthy RUNNING GKE pool state and THIS pool's requested
    Ready-node count (its own label_selector) to converge — the adopt path only
    imports+refreshes (never applies), so this gate is what stops an unready
    RECONCILING/ERROR adopted CPU pool from reporting success.
A timeout, unhealthy pool state, labeling error, missing node, or readback mismatch
keeps step success false.

For a GPU pool the zone is chosen by the same capacity preflight setup uses (a
GKE node-pool CREATE op cannot be cancelled, so the pool is never created in an
unprobed zone).

Emits the `node_pool` schema the suite reads via {{steps.<step>.*}}.

AWS reference: ../../aws/scripts/eks/create_node_pool.sh
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_lib as k8s

PLATFORM = "kubernetes"

# Internal apply budgets (bounded); the config step timeout is headroom over the
# preflight (GPU only) + apply + kubeconfig + pool readiness+bridge worst-case sum.
# A CPU pool applies quickly. A GPU pool's apply MUST exceed the google provider's
# own node-pool create timeout (30m default) so a definitive GKE terminal error
# surfaces instead of the wrapper killing a still-running create. Both stay under
# their config step timeout (never an outer step timeout shorter than this inner
# apply timeout).
_APPLY_TIMEOUT = 1500
_GPU_APPLY_TIMEOUT = 3000

# Pool-scoped GPU completion-gate budget (GPU only): after apply, wait for THIS
# pool's nodes to be Ready with allocatable nvidia.com/gpu, then bridge + read
# back nvidia.com/gpu.present=true. Kept well under the config step cap (3000s)
# minus the preflight + apply + kubeconfig worst case (see config/k8s.yaml).
_GPU_POOL_READY_TIMEOUT = 360

# Pool-scoped CPU completion-gate budget (CPU pools): after apply/adopt, require
# the live GKE pool to reach a healthy RUNNING state and THIS pool's requested
# Ready node count to converge. CPU nodes register Ready within a couple minutes;
# this stays well under the CPU step cap (1800s) and under the inner apply budget
# (never an outer step timeout shorter than any inner wait).
_CPU_POOL_READY_TIMEOUT = 240


def _parse_json_object(raw: str, flag: str) -> dict[str, Any]:
    # Python argparse carries the value verbatim, so the bash `${VAR:-{}}`
    # brace-default pitfall cannot occur here; we still validate the SHAPE and
    # emit a clear config_error so a malformed operator value fails loudly.
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise k8s.LifecycleError("config_error", f"[bucket=config_error] {flag} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise k8s.LifecycleError("config_error", f"[bucket=config_error] {flag} must be a JSON object.")
    return value


def _parse_json_array(raw: str, flag: str) -> list[Any]:
    try:
        value = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise k8s.LifecycleError("config_error", f"[bucket=config_error] {flag} is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise k8s.LifecycleError("config_error", f"[bucket=config_error] {flag} must be a JSON array.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or scale a GKE test node pool via Terraform.")
    parser.add_argument("--pool-name", required=True, help="Node-pool name base (RUN_ID-suffixed by the stub).")
    parser.add_argument("--node-count", type=int, default=1, help="Desired node count (bump on update to scale).")
    parser.add_argument("--machine-type", required=True, help="node_config.machine_type for the pool.")
    parser.add_argument("--accelerator-type", default="", help="guest_accelerator.type (GPU pool only).")
    parser.add_argument("--accelerator-count", type=int, default=0, help="GPUs per node (GPU pool only).")
    parser.add_argument(
        "--gpu-node-locations",
        default="",
        help="Ordered comma-separated candidate zones for the GPU pool preflight (empty -> the cluster location zone).",
    )
    parser.add_argument("--labels-json", default="{}", help="JSON object of custom node labels.")
    parser.add_argument("--taints-json", default="[]", help="JSON array of taints (Kubernetes effect spelling).")
    args = parser.parse_args()

    is_gpu = bool(args.accelerator_type.strip()) and args.accelerator_count > 0
    node_type = "gpu" if is_gpu else "cpu"

    result: dict[str, Any] = {"success": False, "platform": PLATFORM, "node_type": node_type}
    try:
        project = k8s.resolve_project_id()
        pool_name = k8s.scoped_name(args.pool_name)
        state_file = k8s.state_file_for_pool(pool_name)

        labels = _parse_json_object(args.labels_json, "--labels-json")
        taints = _parse_json_array(args.taints_json, "--taints-json")

        # Read the primary cluster wiring once. It seeds the GPU preflight zone
        # fallback / the CPU-pool single-zone pin, AND is threaded back into this
        # pool's OWN state (cluster_name / cluster_location) so a later var-less
        # destroy resolves even after the primary state's outputs are gone.
        cluster_name = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "cluster_name")
        cluster_location = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "location")

        # FAIL CLOSED before ANY pool lookup, refresh, import, or Terraform apply:
        # prove the parent cluster still carries THIS run's exact full-run-identity
        # ownership marker. The pool lives INSIDE this cluster, and neither local
        # pool state (in_state) nor a bare run-scoped name match proves the cluster
        # is the one this run created — a state-tracked cluster that was deleted and
        # replaced by a colliding same-name FOREIGN cluster would otherwise let a
        # stale pool state create, scale, or reconcile a pool inside that foreign
        # cluster. Every mutation path (fresh create, in-state reconcile/scale, and
        # adopt) passes through this single gate, so no path can mutate a pool in a
        # cluster this run does not own.
        k8s.verify_cluster_ownership(cluster_name, cluster_location, project)

        # The harness preserves run-scoped resources (GCP_K8S_SKIP_TEARDOWN), so
        # this pool may already exist from an earlier per-step worker in the run
        # while THIS worktree's local state is empty; a blind create would collide
        # on 409 "already exists". Detect that and ADOPT it (import) instead. A pool
        # already tracked in the local state (created/adopted earlier in this same
        # worktree — e.g. the CPU pool that update_test_node_pool then scales)
        # reconciles in place.
        in_state = k8s.terraform_state_has(k8s.NODE_POOL_TF_DIR, state_file, "google_container_node_pool.this")
        pool_in_cloud = in_state or k8s.gke_node_pool_exists(cluster_name, pool_name, cluster_location, project)
        adopt = pool_in_cloud and not in_state

        node_locations: list[str]
        if is_gpu:
            if pool_in_cloud:
                # Reuse the existing GPU pool's ACTUAL zone: never re-run the
                # non-deterministic preflight against a pool whose zone is fixed
                # (a different node_locations would drift/replace it).
                gpu_zone = k8s.gke_node_pool_zone(cluster_name, pool_name, cluster_location, project) or ""
            else:
                gpu_zone = ""
            if not gpu_zone:
                # Read the selected VPC from the primary cluster's Terraform state so
                # the GPU capacity probe runs on the SAME network the cluster attaches
                # to — never an independent `default` fallback that could drift to a
                # different VPC in a custom-network project.
                cluster_network = k8s.normalize_network(
                    k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, k8s.cluster_state_file(), "network")
                )
                candidates = k8s.candidate_gpu_zones(args.gpu_node_locations, cluster_location)
                gpu_zone = k8s.select_gpu_zone(
                    project,
                    candidates,
                    args.machine_type,
                    args.accelerator_type,
                    args.accelerator_count,
                    network=cluster_network,
                )
            node_locations = [gpu_zone]
            result["gpu_zone"] = gpu_zone
        else:
            # Pin the CPU test pool to ONE zone derived from the cluster location so
            # a REGIONAL cluster does not multiply node_count across every region
            # zone (node_count is PER-ZONE) — keeps the actual Ready node count equal
            # to the emitted expected_replicas. A zonal cluster is unchanged.
            node_locations = [k8s.zone_for_location(cluster_location)]

        k8s.terraform_init(k8s.NODE_POOL_TF_DIR)
        tf_vars = {
            "project": project,
            "pool_name": pool_name,
            "cluster_state_path": k8s.cluster_state_path_for_node_pool(),
            "cluster_name": cluster_name,
            "cluster_location": cluster_location,
            "node_count": args.node_count,
            "machine_type": args.machine_type,
            "node_type": node_type,
            "accelerator_type": args.accelerator_type,
            "accelerator_count": args.accelerator_count if is_gpu else 0,
            "node_locations": node_locations,
            "labels": labels,
            "taints": taints,
        }
        apply_timeout = _GPU_APPLY_TIMEOUT if is_gpu else _APPLY_TIMEOUT
        if adopt:
            # Adopt the preserved pool: import it, then refresh-only to populate the
            # node_pool outputs WITHOUT modifying a pool another worker owns (never
            # scale/replace it here). A later same-worktree step that must change it
            # (update_test_node_pool scaling the CPU pool) then sees it in-state and
            # reconciles in place via the apply branch below.
            #
            # Parent-cluster ownership was already proven above (before any pool
            # lookup), so this import is safe: the pool lives inside a cluster this
            # run demonstrably owns. Importing a pool under a cluster this run did not
            # own would make the pool eligible for teardown's destroy on a bare name
            # match — the exact case the up-front verify_cluster_ownership gate blocks.
            pool_id = f"projects/{project}/locations/{cluster_location}/clusters/{cluster_name}/nodePools/{pool_name}"
            k8s.terraform_import(k8s.NODE_POOL_TF_DIR, state_file, "google_container_node_pool.this", pool_id, tf_vars)
            k8s.terraform_refresh_only(k8s.NODE_POOL_TF_DIR, state_file, tf_vars)
        else:
            # Fresh create, or an in-state pool this worktree already owns (the CPU
            # pool that update_test_node_pool scales in place).
            k8s.terraform_apply(k8s.NODE_POOL_TF_DIR, state_file, tf_vars, timeout=apply_timeout)

        # Read back what Terraform actually created.
        node_pool_name = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "node_pool_name")
        label_selector = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "label_selector")
        desired_size = int(k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "desired_size"))
        expected_labels = k8s.terraform_output_json(k8s.NODE_POOL_TF_DIR, state_file, "expected_labels")
        expected_taints = k8s.terraform_output_json(k8s.NODE_POOL_TF_DIR, state_file, "expected_taints")
        expected_instance_types = k8s.terraform_output_json(k8s.NODE_POOL_TF_DIR, state_file, "expected_instance_types")

        if adopt:
            # The expected_* outputs above are derived from Terraform INPUT vars, not
            # the live pool. Prove the ADOPTED pool actually has that shape before
            # emitting it, so a preserved same-name pool with a different shape can
            # never be reported with a fabricated contract the released
            # K8sNodePoolCheck then asserts against. Verify machine type + every
            # expected label/taint AND the requested node count, zone placement, and
            # GPU accelerator type/count — the emitted expected_replicas is read from
            # the pool's OWN refreshed state, so without a live-vs-requested count
            # gate an adopted pool would seed its own replica expectation.
            k8s.verify_adopted_node_pool_shape(
                cluster_name,
                pool_name,
                cluster_location,
                project,
                args.machine_type,
                expected_labels,
                expected_taints,
                expected_node_count=args.node_count,
                expected_node_locations=node_locations,
                expected_accelerator_type=args.accelerator_type.strip() if is_gpu else "",
                expected_accelerator_count=args.accelerator_count if is_gpu else 0,
            )

        if is_gpu:
            # Ensure kubectl reaches the cluster (reusing the wiring read above),
            # then run the POOL-SCOPED GPU completion gate: block until THIS pool's
            # own nodes (label_selector) are Ready with allocatable nvidia.com/gpu,
            # apply nvidia.com/gpu.present=true to exactly those nodes, and read the
            # label back. A timeout, labeling error, missing node, or readback
            # mismatch RAISES here, so success stays False and the released GPU
            # checks never run against an unready or undiscoverable test pool. Each
            # GPU pool gates on its OWN selector (setup's baseline pool and every
            # test pool alike), so another pool's Ready nodes can never satisfy this
            # pool's readiness count.
            k8s.install_kubeconfig(cluster_name, cluster_location, project)
            k8s.wait_gpu_pool_ready_and_bridge(label_selector, desired_size, timeout=_GPU_POOL_READY_TIMEOUT)
        else:
            # CPU pool completion gate — the SAME live completion contract the GPU
            # path and the fresh Terraform apply enforce, applied to EVERY CPU path
            # (fresh create, in-state scale, AND adopt). On the adopt path the pool is
            # only imported + refreshed (never applied), so without this gate an
            # adopted CPU pool would emit success from static shape alone while it was
            # still RECONCILING/ERROR or short of its Ready node count. Ensure kubectl
            # reaches the cluster, then require a healthy RUNNING GKE pool state and
            # THIS pool's requested Ready-node count (its own label_selector) to
            # converge before emitting success. A timeout, terminal-error pool state,
            # or too-few Ready nodes RAISES here, keeping step success false.
            k8s.install_kubeconfig(cluster_name, cluster_location, project)
            k8s.wait_cpu_pool_ready(
                cluster_name,
                pool_name,
                cluster_location,
                project,
                label_selector,
                desired_size,
                timeout=_CPU_POOL_READY_TIMEOUT,
            )

        # Store labels/taints/instance-types as JSON STRINGS: Jinja renders step
        # outputs as strings, so the check json-parses these back.
        result.update(
            {
                "success": True,
                "node_pool_name": node_pool_name,
                "label_selector": label_selector,
                "expected_replicas": desired_size,
                "expected_labels_json": json.dumps(expected_labels, separators=(",", ":")),
                "expected_taints_json": json.dumps(expected_taints, separators=(",", ":")),
                "expected_instance_types_json": json.dumps(expected_instance_types, separators=(",", ":")),
                "node_type": node_type,
            }
        )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc, node_type=node_type)

    return k8s.emit(result)


if __name__ == "__main__":
    sys.exit(main())
