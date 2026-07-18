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

"""GKE cluster setup (setup phase) — the GCP analog of the AWS EKS oracle setup.

Provisions the primary GKE cluster via Terraform (official hashicorp/google
provider), installs the kubeconfig with `gcloud container clusters
get-credentials` (ambient kubectl), gates on the GPU driver finishing (two-gate
preflight), then emits the `cluster` inventory observed live via kubectl.

Cluster shape (see terraform/main.tf):
  * remove_default_node_pool + a separately-named system (CPU) node pool.
  * a baseline GPU node pool whose zone is chosen by a capacity preflight probe
    (a standalone size-1 MIG mirroring the GPU shape — a GKE node-pool CREATE op
    cannot be cancelled, so the pool is never created in an unprobed zone).
  * Dataplane V2 so Kubernetes NetworkPolicy is enforced natively.
  * control-plane logging enabled (Cloud Logging) + managed Prometheus DISABLED.

Post-provision bootstrap before inventory:
  * apply a passthrough `nvidia` RuntimeClass (handler `runc`) so the released
    GPU-workload manifests that pin `runtimeClassName: nvidia` schedule on the
    managed-driver GPU path without installing the GPU Operator.
  * label GKE GPU nodes nvidia.com/gpu.present=true (what the released GPU checks
    select on).
  * two-gate preflight: a GPU node Ready + allocatable nvidia.com/gpu AND a
    responding nvidia-smi driver, BEFORE emitting inventory.

AWS reference: ../../aws/scripts/eks/setup.sh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_lib as k8s

PLATFORM = "kubernetes"

# Internal wait budgets (all bounded; the config step timeout is headroom over
# their worst-case SUM, not their product). The apply budget MUST exceed the
# google provider's own cluster/node-pool create timeouts (40m cluster / 30m
# pool) so a definitive GKE terminal error surfaces instead of the wrapper
# killing a still-running create; the config step timeout is in turn larger than
# this inner apply budget (never an outer step timeout shorter than the inner).
#   preflight probe (~3 zones * ~270s)      ~=  810s
#   cluster + baseline GPU pool apply        <= 3600s
#   kubeconfig install                       <=  180s
#   network verify + RuntimeClass apply      <=  180s
#   system-pool autoscaling readback/reconcile <=  600s
#   GPU readiness gate (provider-native)     <=  900s
#   MPI Operator apply + readiness gate      <=  300s
#   inventory (kubectl reads + pod exec)     <=  300s
# Worst-case serial sum ~= 6870s -> config setup step timeout 7200s.
_APPLY_TIMEOUT = 3600
_TWO_GATE_TIMEOUT = 900
_AUTOSCALING_TIMEOUT = 600
_MPI_OPERATOR_TIMEOUT = 300


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision the primary GKE cluster and emit inventory.")
    parser.add_argument("--cluster-name", default="isv-gke", help="Cluster name base (RUN_ID-suffixed by the stub).")
    parser.add_argument("--location", required=True, help="GKE location (region or zone).")
    parser.add_argument("--kube-version", default="", help="min_master_version pin; empty -> REGULAR channel default.")
    parser.add_argument("--cpu-machine-type", required=True, help="Machine type for the system (CPU) node pool.")
    parser.add_argument("--gpu-machine-type", required=True, help="Machine type for the baseline GPU node pool.")
    parser.add_argument(
        "--gpu-accelerator-type", required=True, help="guest_accelerator.type for the baseline GPU pool."
    )
    parser.add_argument("--gpu-accelerator-count", type=int, default=1, help="GPUs per node.")
    parser.add_argument("--gpu-node-count", type=int, default=1, help="Baseline GPU pool node count.")
    parser.add_argument(
        "--gpu-node-locations",
        default="",
        help="Ordered comma-separated candidate zones for the GPU capacity preflight (empty -> the location zone).",
    )
    parser.add_argument(
        "--network",
        default="default",
        help="VPC network shared by the cluster and the GPU capacity-preflight MIG (blank/'none' -> default).",
    )
    parser.add_argument(
        "--authorized-cidrs",
        default="",
        help="Comma-separated GKE control-plane authorized CIDRs; blank/'none' sentinel -> disabled.",
    )
    parser.add_argument(
        "--unauthorized-probe-template",
        default="",
        help="Outside-vantage probe template containing {api_endpoint}; blank/'none' sentinel -> disabled.",
    )
    parser.add_argument("--system-node-count", type=int, default=1, help="System (CPU) pool seed node count.")
    parser.add_argument(
        "--system-min-nodes", type=int, default=1, help="Lower bound for the managed autoscaler on the system pool."
    )
    parser.add_argument(
        "--system-max-nodes", type=int, default=3, help="Upper bound for the managed autoscaler on the system pool."
    )
    args = parser.parse_args()

    result: dict = {"success": False, "platform": PLATFORM}
    try:
        # Managed-autoscaler bound relationship is enforced HERE (not in a
        # Terraform variable validation): a cross-variable `condition` referencing
        # var.system_min_nodes from var.system_max_nodes's validation is a
        # Terraform 1.9+ language feature, but the module advertises a >=1.5 floor.
        # Keep the module self-contained on 1.5 by checking the relationship in the
        # stub before apply.
        if args.system_max_nodes < args.system_min_nodes:
            raise k8s.LifecycleError(
                "config_error",
                "[bucket=config_error] GCP_K8S_SYSTEM_MAX_NODES "
                f"({args.system_max_nodes}) must be >= GCP_K8S_SYSTEM_MIN_NODES "
                f"({args.system_min_nodes}); the GKE-managed autoscaler upper bound cannot "
                "be below its lower bound.",
            )

        project = k8s.resolve_project_id()
        cluster_name = k8s.scoped_name(args.cluster_name)

        # Resolve the canonical baseline pool names ONCE (the same trimmed/capped
        # spelling terraform's locals produce) and reuse them for the GPU-zone
        # lookup, the adopt import, and the autoscaling readback — never a raw
        # f"{cluster_name}-gpu", which diverges from the canonical name when an
        # operator-supplied cluster base hits the GKE 40-char pool-name cap.
        system_pool_name, gpu_pool_name = k8s.baseline_pool_names(cluster_name)

        # Resolve the operator network / API-ACL capability inputs. The provider
        # config renders the non-empty "none" sentinel (so the arg renderer never
        # drops an empty value token) for the two optional API-ACL inputs; setup
        # normalizes each back to absence. authorized_cidrs rejects world-open
        # ranges (fail closed, never a silent ACL bypass).
        network = k8s.normalize_network(args.network)
        authorized_cidrs = k8s.normalize_authorized_cidrs(args.authorized_cidrs)
        probe_template = k8s.normalize_sentinel(args.unauthorized_probe_template)

        # The outside-vantage probe only proves ACL ENFORCEMENT against a real
        # authorized-network policy. Requiring the probe template WITHOUT any
        # authorized CIDRs would leave the endpoint open (no block rendered), so a
        # failing probe would prove nothing. Fail CLOSED on that pairing gap.
        if probe_template and not authorized_cidrs:
            raise k8s.LifecycleError(
                "config_error",
                "[bucket=config_error] GCP_K8S_UNAUTHORIZED_PROBE_CMD is set but "
                "GCP_K8S_AUTHORIZED_CIDRS is empty. An unauthorized probe cannot prove "
                "API-ACL enforcement when the control-plane endpoint has no authorized "
                "networks configured; supply the runner's egress CIDR(s) or unset the probe.",
            )

        # The harness preserves every run-scoped resource (GCP_K8S_SKIP_TEARDOWN),
        # so an earlier per-step worker in THIS run may already have provisioned the
        # run-scoped cluster. A fresh worktree's local state is empty, so a blind
        # `terraform apply` would re-CREATE it and collide on 409 "already exists".
        # Detect the existing cluster and ADOPT it below instead.
        state_file = k8s.cluster_state_file()
        cluster_in_state = k8s.terraform_state_has(k8s.CLUSTER_TF_DIR, state_file, "google_container_cluster.primary")
        # Query cloud existence INDEPENDENTLY of local state: state alone must not
        # select the adopt/refresh-only branch. A cluster tracked in stale state but
        # genuinely GONE from the cloud (deleted out of band, or a partial prior
        # create) must be RECREATED, not refreshed into a phantom that later
        # readiness/autoscaling checks fail on. Discard the stale state so the
        # fresh-create path below rebuilds the state-owned cluster + baseline pools.
        cluster_on_cloud = k8s.gke_cluster_exists(cluster_name, args.location, project)
        if cluster_in_state and not cluster_on_cloud:
            k8s.log(
                f"note: Terraform state tracks cluster {cluster_name} but it is absent from the "
                "cloud; discarding stale state and recreating the state-owned cluster."
            )
            k8s.discard_cluster_state(k8s.CLUSTER_TF_DIR, state_file)
            cluster_in_state = False
        cluster_exists = cluster_on_cloud

        # 1) GPU zone. A FRESH create runs the capacity preflight BEFORE the cluster
        #    apply (a GKE node-pool CREATE op cannot be cancelled). An ALREADY
        #    provisioned cluster reuses its baseline GPU pool's ACTUAL zone so the
        #    adopt/reconcile never drifts the pool onto a different zone (the probe
        #    is non-deterministic). The probe runs on the operator-selected VPC so a
        #    fresh create proves the shape is placeable in the cluster's substrate.
        if cluster_exists:
            gpu_zone = ""
            if cluster_in_state:
                try:
                    gpu_zone = k8s.terraform_output_raw(k8s.CLUSTER_TF_DIR, state_file, "gpu_zone")
                except k8s.LifecycleError:
                    gpu_zone = ""
            if not gpu_zone:
                gpu_zone = k8s.gke_node_pool_zone(cluster_name, gpu_pool_name, args.location, project) or ""
            if not gpu_zone:
                # Cluster exists but its baseline GPU pool zone is unreadable (e.g. a
                # partial prior create); fall back to the preflight so a genuine
                # reconcile apply can still place the pool.
                candidates = k8s.candidate_gpu_zones(args.gpu_node_locations, args.location)
                gpu_zone = k8s.select_gpu_zone(
                    project,
                    candidates,
                    args.gpu_machine_type,
                    args.gpu_accelerator_type,
                    args.gpu_accelerator_count,
                    network=network,
                )
        else:
            candidates = k8s.candidate_gpu_zones(args.gpu_node_locations, args.location)
            gpu_zone = k8s.select_gpu_zone(
                project,
                candidates,
                args.gpu_machine_type,
                args.gpu_accelerator_type,
                args.gpu_accelerator_count,
                network=network,
            )

        # 2) Provision (fresh) or adopt (existing) the cluster + system pool +
        #    baseline GPU pool. Pin the system pool to ONE zone derived from the
        #    location (like the GPU pool) so a REGIONAL cluster does not multiply
        #    system node_count across every region zone (node_count is PER-ZONE); a
        #    zonal location is unchanged. network attaches the cluster;
        #    master_authorized_cidrs enables GKE authorized networks (empty -> block
        #    omitted, public endpoint open).
        k8s.terraform_init(k8s.CLUSTER_TF_DIR)
        tf_vars = {
            "project": project,
            "cluster_name": cluster_name,
            "location": args.location,
            "kube_version": args.kube_version,
            "network": network,
            # Stamp the full-run-identity ownership marker on the cluster ATOMICALLY
            # at creation (resource_labels) so a genuinely run-owned cluster always
            # carries it — the adopt/relabel/destroy paths then treat an absent or
            # mismatched marker as a foreign/replaced cluster and fail closed.
            "ownership_labels": {k8s.OWNERSHIP_LABEL_KEY: k8s.full_run_scope_id()},
            "master_authorized_cidrs": authorized_cidrs,
            "system_machine_type": args.cpu_machine_type,
            "system_node_count": args.system_node_count,
            # GKE-managed autoscaling bounds for the CPU/system pool (GPU pools
            # stay fixed). Read back + verified live after apply/adopt below.
            "system_min_nodes": args.system_min_nodes,
            "system_max_nodes": args.system_max_nodes,
            "system_node_locations": [k8s.zone_for_location(args.location)],
            "gpu_machine_type": args.gpu_machine_type,
            "gpu_accelerator_type": args.gpu_accelerator_type,
            "gpu_accelerator_count": args.gpu_accelerator_count,
            "gpu_node_count": args.gpu_node_count,
            "gpu_node_locations": [gpu_zone],
        }
        if cluster_exists:
            # ADOPT the preserved run-scoped cluster AND its baseline system/GPU
            # node pools. The harness keeps every run resource alive
            # (GCP_K8S_SKIP_TEARDOWN), so a fresh worktree's empty local state — or a
            # PARTIAL prior import that captured only the cluster — makes a normal
            # `terraform apply` try to re-CREATE the already-live cluster/pools and
            # collide on 409 "already exists". Import every managed resource the
            # cloud has but local state lacks, then refresh-only to populate outputs
            # WITHOUT a create/replace: a normal apply is unsafe on an imported
            # cluster because the provider reads initial_node_count back as 0 after
            # remove_default_node_pool and would force a full cluster REPLACE.
            cluster_id = f"projects/{project}/locations/{args.location}/clusters/{cluster_name}"
            # FAIL CLOSED before ANY state-backed import, pool recreation, or
            # refresh touches the live cluster — on BOTH the fresh-worktree adopt
            # path AND the in-state re-entry path. Prove exact ownership by the FULL
            # run identity via the cloud-side marker first (a genuinely run-owned
            # cluster is stamped at Terraform creation, so this is a no-op read on
            # the common path). A deleted-and-replaced same-name FOREIGN cluster —
            # even one this worktree's stale local state still tracks — must never
            # have its baseline pools imported/recreated/refreshed (and later
            # destroyed) as though this run owned it. A bare run-scoped name or a
            # local-state entry must never authorize mutating the live cluster.
            k8s.verify_cluster_ownership(cluster_name, args.location, project)
            if not cluster_in_state:
                # Before importing a cluster this worktree's state does not yet
                # track, also verify the contract-relevant network BEFORE the import
                # makes the cluster eligible for teardown's destroy, so an adopted
                # cluster on an unexpected substrate is never imported with a
                # mismatched network.
                observed_net, _observed_subnet, _status = k8s.read_cluster_membership(
                    cluster_name, args.location, project
                )
                if not k8s.same_network(observed_net, network):
                    raise k8s.LifecycleError(
                        "config_error",
                        f"[bucket=config_error] refusing to adopt cluster {cluster_name}: it is "
                        f"attached to network '{observed_net}', not the operator-selected VPC "
                        f"'{network}'. An adopted cluster on an unexpected network would be "
                        "imported (and later destroyed) with a mismatched substrate.",
                    )
                k8s.terraform_import(
                    k8s.CLUSTER_TF_DIR, state_file, "google_container_cluster.primary", cluster_id, tf_vars
                )
            # The baseline pools are declared INSIDE the cluster module (no separate
            # tfstate), so importing the cluster alone leaves them untracked and a
            # later reconcile apply tries to CREATE the already-live pools (the
            # observed 409). Import each pool the cloud has but local state lacks, by
            # its terraform-derived name (resolved once at the top of setup).
            for address, pool_name in (
                ("google_container_node_pool.system", system_pool_name),
                ("google_container_node_pool.gpu", gpu_pool_name),
            ):
                if k8s.terraform_state_has(k8s.CLUSTER_TF_DIR, state_file, address):
                    continue
                if not k8s.gke_node_pool_exists(cluster_name, pool_name, args.location, project):
                    # The cluster exists but this REQUIRED baseline pool is genuinely
                    # absent (a partial prior create, or an out-of-band pool delete). A
                    # refresh-only would leave it missing and later readiness/autoscaling
                    # checks would fail loudly instead of restoring the contract, so
                    # RECREATE it via the API (a node-pool create can only ADD the pool
                    # and can never replace the cluster), then import so state tracks the
                    # rebuilt pool.
                    if address == "google_container_node_pool.system":
                        k8s.recreate_baseline_system_pool(
                            cluster_name,
                            pool_name,
                            args.location,
                            project,
                            machine_type=args.cpu_machine_type,
                            node_zone=k8s.zone_for_location(args.location),
                            node_count=args.system_node_count,
                            min_nodes=args.system_min_nodes,
                            max_nodes=args.system_max_nodes,
                        )
                    else:
                        k8s.recreate_baseline_gpu_pool(
                            cluster_name,
                            pool_name,
                            args.location,
                            project,
                            machine_type=args.gpu_machine_type,
                            node_zone=gpu_zone,
                            node_count=args.gpu_node_count,
                            accelerator_type=args.gpu_accelerator_type,
                            accelerator_count=args.gpu_accelerator_count,
                        )
                pool_id = f"{cluster_id}/nodePools/{pool_name}"
                k8s.terraform_import(k8s.CLUSTER_TF_DIR, state_file, address, pool_id, tf_vars)
            k8s.terraform_refresh_only(k8s.CLUSTER_TF_DIR, state_file, tf_vars)
        else:
            # Fresh create (the cluster does not exist yet — nothing to adopt).
            k8s.terraform_apply(k8s.CLUSTER_TF_DIR, state_file, tf_vars, timeout=_APPLY_TIMEOUT)

        # 2a) Confirm the cloud-side full-run-identity ownership marker. The cluster
        #     is stamped ATOMICALLY at Terraform creation (resource_labels), so this
        #     is a no-op read on the common path; it FAILS CLOSED if the live marker
        #     belongs to a different run (a deleted-and-replaced same-name cluster) or
        #     is absent on an adopted cluster, and only backfills a marker on a fresh
        #     create (belt-and-suspenders if the label write has not yet propagated).
        k8s.ensure_cluster_ownership_label(cluster_name, args.location, project, fresh_create=not cluster_exists)

        # 3) Install the kubeconfig for ambient kubectl.
        k8s.install_kubeconfig(cluster_name, args.location, project)

        # 3a) Read + verify the live cluster network/subnetwork (fail if it does
        #     not match the operator-selected VPC), so success is derived from real
        #     state, not just the Terraform input.
        observed_network, observed_subnetwork = k8s.verify_and_read_network(
            cluster_name, args.location, project, network
        )

        # 3a-i) When the operator configured an API-ACL policy, read back the LIVE
        #     master_authorized_networks source set and require it to equal the
        #     requested CIDRs (fresh create AND adopt). A fresh apply could omit the
        #     block, and a reused/adopted cluster could enforce a DIFFERENT allow-
        #     list — either way an unrelated probe failure would misread as ACL
        #     enforcement. Fail CLOSED on a missing/mismatched policy.
        if authorized_cidrs:
            k8s.verify_authorized_networks(cluster_name, args.location, project, authorized_cidrs)

        # 3a-ii) Read back + verify GKE-managed autoscaling on the system pool
        #     (enable/reconcile it on the adopt path) and capture the provider-
        #     native evidence for inventory. Fails CLOSED unless the live pool is
        #     autoscaling with the requested min/max bounds.
        autoscaler_evidence = k8s.verify_system_autoscaling(
            cluster_name,
            system_pool_name,
            args.location,
            project,
            args.system_min_nodes,
            args.system_max_nodes,
            timeout=_AUTOSCALING_TIMEOUT,
        )

        # 3b) Resolve the reviewed cluster's API server URL and bind the ACL probe
        #     to it. K8sApiNetworkAclCheck only runs its target-origin and
        #     kubeconfig-consistency guards when api_endpoint is set; without it an
        #     enabled unauthorized_probe that trivially fails against a typo, stale,
        #     or unrelated host would be misread as "ACL enforced". When the
        #     operator has enabled the probe, fail CLOSED if the endpoint cannot be
        #     resolved rather than emit a security PASS that never probed this
        #     cluster. render_unauthorized_probe substitutes {api_endpoint}.
        api_endpoint = k8s.resolve_api_endpoint(cluster_name, args.location, project)
        if probe_template and not api_endpoint:
            raise k8s.LifecycleError(
                "config_error",
                "[bucket=config_error] GCP_K8S_UNAUTHORIZED_PROBE_CMD is set but the "
                "cluster API endpoint could not be resolved from the installed "
                "kubeconfig or the GKE API. Refusing to emit an unbound ACL probe: "
                "an unauthorized probe scored without a verified target endpoint "
                "could report a false security PASS. Verify kubectl/gcloud access "
                "to the cluster and retry.",
            )
        unauthorized_probe_cmd = k8s.render_unauthorized_probe(probe_template, api_endpoint or "")

        # 4) Honest passthrough RuntimeClass + GPU node labeling bridges.
        k8s.apply_nvidia_runtimeclass()

        # Strip the reserved isv.ncp.validation/pool marker from the baseline
        # system / GPU nodes. terraform no longer sets it, but an ADOPTED
        # (preserved) cluster still carries the old label on its live baseline
        # nodes (adopt uses refresh-only). isvtest's CSI probe pods require that
        # key to NOT exist on their target node, so a lingering baseline marker
        # would make every CSI check unschedulable. No-op on a fresh cluster.
        k8s.strip_baseline_pool_markers()

        # 5) Provider-native GPU readiness gate BEFORE emitting inventory. Scoped
        #    to the baseline GPU pool and gated on ALL gpu_node_count nodes (node
        #    Ready + allocatable nvidia.com/gpu + Ready managed device-plugin pod;
        #    no image pull), so setup never derives inventory from a single
        #    first-ready node or a preserved validation pool. Returns the real
        #    nvidia-smi driver version read from the managed pod, or None.
        driver_version = k8s.wait_two_gate_gpu_ready(gpu_pool_name, args.gpu_node_count, timeout=_TWO_GATE_TIMEOUT)

        # 5a) Install the pinned, vendored Kubeflow MPI Operator (v2beta1) and gate
        #     on its CRD + controller readiness so the released multi-node NCCL
        #     workload (K8sNcclMultiNodeWorkload) has its MPIJob controller instead
        #     of silently structured-skipping. GKE does not install it by default;
        #     the manifest is a local provider asset (never fetched at runtime).
        k8s.install_mpi_operator(timeout=_MPI_OPERATOR_TIMEOUT)

        # 6) Observe inventory live via kubectl. Required node/GPU reads raise on
        #    failure, so setup can never emit synthetic 'success' inventory.
        inventory = k8s.gather_inventory(
            cluster_name,
            driver_version=driver_version,
            api_endpoint=api_endpoint,
            unauthorized_probe_cmd=unauthorized_probe_cmd,
            autoscaler=autoscaler_evidence,
        )

        k8s_block = inventory["kubernetes"]
        result.update(
            {
                "success": True,
                "cluster_name": cluster_name,
                # The canonically-resolved project (explicit -> GOOGLE_CLOUD_PROJECT
                # / GCLOUD_PROJECT -> google.auth.default() ADC, via resolve_project).
                # Downstream telemetry checks (K8sControlPlaneLogsCheck) bind their
                # gcloud-logging --project to {{steps.setup.project}} so every query
                # scopes to the SAME project the cluster was provisioned in, instead
                # of re-deriving it from a partial GOOGLE_CLOUD_PROJECT/gcloud-config
                # chain that omits GCLOUD_PROJECT + ADC.
                "project": project,
                # Live cluster network/subnetwork observed + verified against the
                # operator-selected VPC (top-level outputs; the shared-VPC step also
                # derives its network from the primary Terraform state).
                "network": observed_network,
                "subnetwork": observed_subnetwork,
                "gpu_zone": gpu_zone,
                # Top-level cluster-schema fields (mirror the EKS oracle): the
                # `cluster` output schema requires a top-level integer node_count.
                "node_count": k8s_block["node_count"],
                "gpu_count": k8s_block["total_gpus"],
                "gpu_per_node": k8s_block["gpu_per_node"],
                "driver_version": k8s_block["driver_version"],
                "kubernetes": k8s_block,
                "csi": inventory["csi"],
            }
        )
    except BaseException as exc:  # always emit structured JSON, never crash without output
        result = k8s.error_result(PLATFORM, exc)

    return k8s.emit(result)


if __name__ == "__main__":
    sys.exit(main())
