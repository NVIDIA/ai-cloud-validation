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

# Primary GKE cluster for ISV Lab validation (the GCP analog of the AWS EKS
# realism oracle at ../../../aws/scripts/eks/terraform). Provisions a
# google_container_cluster via the official hashicorp/google provider — the
# REAL create/scale/destroy path for the cluster lifecycle, driven by
# setup.py / teardown.py through a local-backend state file threaded across the
# separate lifecycle-step processes.
#
# The cluster is created with:
#   - remove_default_node_pool + a small separately-named system (CPU) pool, so
#     no unmanaged default pool exists.
#   - a baseline GPU node pool whose zone the stub picks by capacity preflight,
#     so the in-cluster GPU checks (nvidia-smi / driver / capacity / pod-access)
#     have Ready GPU nodes before setup emits inventory.
#   - Dataplane V2 (datapath_provider = ADVANCED_DATAPATH) so Kubernetes
#     NetworkPolicy is ENFORCED natively (K8sNetworkPolicyCheck has no skip path).
#   - control-plane logging components enabled so K8sControlPlaneLogsCheck can
#     read apiserver/scheduler/controller-manager logs from Cloud Logging.
#   - managed_prometheus DISABLED: its gmp-system/collector DaemonSet does not
#     tolerate the dedicated NoSchedule taint on the CPU test pool and would sit
#     Pending, failing K8sNoPendingPodsCheck. No released k8s check consumes GMP.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "google" {
  project = var.project
}

locals {
  # Derive run-scoped pool names from the already run-scoped cluster_name so
  # every named resource the apply creates carries the run id (never a static
  # module default that would collide across concurrent runs).
  #
  # GKE node-pool names are RFC-1035 capped at 40 chars, and cluster_name is
  # itself already capped at 40 — so blindly appending a "-<role>" suffix could
  # overflow for a long direct cluster-name override. Cap each INDIRECT pool name
  # independently, trimming only the cluster BASE (never the run-id tail or the
  # role discriminator): the common short-name case keeps the familiar
  # "<cluster>-<role>" spelling, while an over-long name falls back to a trimmed
  # "<base>-<role>-<sid>" that still preserves both the role and the run id.
  _np_max         = 40
  _cluster_sid    = element(split("-", var.cluster_name), length(split("-", var.cluster_name)) - 1)
  _cluster_base   = trimsuffix(var.cluster_name, "-${local._cluster_sid}")
  _pool_base_keep = max(1, local._np_max - length(local._cluster_sid) - 5) # "-<role(3)>-<sid>"
  _pool_base      = substr(local._cluster_base, 0, min(length(local._cluster_base), local._pool_base_keep))

  system_pool_name = length("${var.cluster_name}-sys") <= local._np_max ? "${var.cluster_name}-sys" : "${local._pool_base}-sys-${local._cluster_sid}"
  gpu_pool_name    = length("${var.cluster_name}-gpu") <= local._np_max ? "${var.cluster_name}-gpu" : "${local._pool_base}-gpu-${local._cluster_sid}"
}

resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.location

  network    = var.network
  subnetwork = var.subnetwork == "" ? null : var.subnetwork

  # No default node pool: create a small, separately-named system pool below.
  remove_default_node_pool = true
  initial_node_count       = 1

  # Pin the control-plane version when the operator supplies one; otherwise
  # ride the REGULAR release channel default.
  min_master_version = var.kube_version == "" ? null : var.kube_version
  dynamic "release_channel" {
    for_each = var.kube_version == "" ? [1] : []
    content {
      channel = "REGULAR"
    }
  }

  # Dataplane V2 enforces Kubernetes NetworkPolicy natively (no separate
  # network_policy block, which conflicts with ADVANCED_DATAPATH).
  datapath_provider = "ADVANCED_DATAPATH"

  # GKE authorized networks: restrict the control-plane PUBLIC endpoint to the
  # operator-approved CIDRs when supplied (K8sApiNetworkAclCheck capability).
  # Empty list -> block omitted (endpoint open); the stub rejects world-open
  # 0.0.0.0/0 before a value ever reaches here.
  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_cidrs) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_cidrs
        content {
          cidr_block = cidr_blocks.value
        }
      }
    }
  }

  # Control-plane logs to Cloud Logging (K8sControlPlaneLogsCheck reads these
  # per-component via `gcloud logging read`).
  logging_config {
    enable_components = [
      "SYSTEM_COMPONENTS",
      "APISERVER",
      "CONTROLLER_MANAGER",
      "SCHEDULER",
      "WORKLOADS",
    ]
  }

  # Keep system monitoring, but DISABLE Managed Prometheus so the
  # gmp-system/collector DaemonSet (which cannot tolerate the CPU test pool's
  # dedicated taint) is never deployed.
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = false
    }
  }

  # A destroy must not be blocked by deletion protection.
  deletion_protection = false
}

# System (CPU) node pool — hosts kube-system / DaemonSet workloads. Untainted
# so system pods schedule freely. PINNED to a single zone (like the GPU pools):
# node_count is PER-ZONE, so on a REGIONAL cluster an unpinned system pool would
# spread node_count across every region zone (node_count x #zones), tripling cost
# and breaking the "CPU/system pool stays single-zone" invariant. Empty ->
# inherit the cluster's node locations (a zonal cluster is already single-zone).
resource "google_container_node_pool" "system" {
  name           = local.system_pool_name
  cluster        = google_container_cluster.primary.name
  location       = google_container_cluster.primary.location
  node_locations = length(var.system_node_locations) > 0 ? var.system_node_locations : null

  node_count = var.system_node_count

  node_config {
    machine_type = var.system_machine_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = {
      "isv.ncp.validation/pool" = "system"
    }
  }
}

# Baseline GPU node pool — a FIXED, SINGLE-ZONE pool whose zone was chosen by a
# capacity preflight so it provisions eagerly (a fixed pool inserts its node
# immediately; the setup GPU preflight then finds a Ready GPU node first-pass).
# Untainted so the released GPU-workload pods schedule here. Drivers install via
# the GKE-managed DaemonSet (kube-system), so no NVIDIA GPU Operator is needed.
resource "google_container_node_pool" "gpu" {
  name           = local.gpu_pool_name
  cluster        = google_container_cluster.primary.name
  location       = google_container_cluster.primary.location
  node_locations = var.gpu_node_locations

  node_count = var.gpu_node_count

  node_config {
    machine_type = var.gpu_machine_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    guest_accelerator {
      type  = var.gpu_accelerator_type
      count = var.gpu_accelerator_count

      # Let GKE install and manage the NVIDIA driver via its DaemonSet.
      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    labels = {
      "isv.ncp.validation/pool" = "baseline-gpu"
    }
  }
}
