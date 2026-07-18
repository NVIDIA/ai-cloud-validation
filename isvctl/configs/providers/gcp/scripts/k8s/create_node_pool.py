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

For a GPU pool the zone is chosen by the same capacity preflight setup uses (a
GKE node-pool CREATE op cannot be cancelled, so the pool is never created in an
unprobed zone). After apply, a POOL-SCOPED completion gate waits for THIS pool's
own nodes (its label_selector) to be Ready with allocatable nvidia.com/gpu, then
applies and reads back nvidia.com/gpu.present=true on exactly those nodes so the
released GPU checks discover them. A timeout, labeling error, missing node, or
readback mismatch keeps step success false.

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

        node_locations: list[str]
        if is_gpu:
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
        k8s.terraform_apply(k8s.NODE_POOL_TF_DIR, state_file, tf_vars, timeout=apply_timeout)

        # Read back what Terraform actually created.
        node_pool_name = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "node_pool_name")
        label_selector = k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "label_selector")
        desired_size = int(k8s.terraform_output_raw(k8s.NODE_POOL_TF_DIR, state_file, "desired_size"))
        expected_labels = k8s.terraform_output_json(k8s.NODE_POOL_TF_DIR, state_file, "expected_labels")
        expected_taints = k8s.terraform_output_json(k8s.NODE_POOL_TF_DIR, state_file, "expected_taints")
        expected_instance_types = k8s.terraform_output_json(k8s.NODE_POOL_TF_DIR, state_file, "expected_instance_types")

        if is_gpu:
            # Ensure kubectl reaches the cluster (reusing the wiring read above),
            # then run the POOL-SCOPED GPU completion gate: block until THIS pool's
            # own nodes (label_selector) are Ready with allocatable nvidia.com/gpu,
            # apply nvidia.com/gpu.present=true to exactly those nodes, and read the
            # label back. A timeout, labeling error, missing node, or readback
            # mismatch RAISES here, so success stays False and the released GPU
            # checks never run against an unready or undiscoverable test pool. The
            # cluster-wide two-gate helper alone would be unsafe: the setup baseline
            # GPU pool could satisfy it while this new test pool is still unready.
            k8s.install_kubeconfig(cluster_name, cluster_location, project)
            k8s.wait_gpu_pool_ready_and_bridge(label_selector, desired_size, timeout=_GPU_POOL_READY_TIMEOUT)

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
