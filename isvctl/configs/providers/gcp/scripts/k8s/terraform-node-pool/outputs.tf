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

# Outputs consumed by create_node_pool.py to build the node_pool JSON payload
# the suite reads via {{steps.create_test_node_pool.*}} /
# {{steps.create_test_gpu_node_pool.*}} / {{steps.update_test_node_pool.*}}.

output "node_pool_name" {
  description = "Name of the created GKE node pool."
  value       = google_container_node_pool.this.name
}

output "label_selector" {
  description = <<-EOT
    kubectl label selector identifying the pool's nodes. GKE always labels
    node-pool nodes with cloud.google.com/gke-nodepool=<name>, so that is the
    stable selector the validation polls on.
  EOT
  value       = "cloud.google.com/gke-nodepool=${google_container_node_pool.this.name}"
}

output "desired_size" {
  description = "Configured node count for the pool."
  value       = google_container_node_pool.this.node_count
}

output "expected_labels" {
  description = "Labels the validation should see on every node (stable markers + caller-supplied)."
  value       = local.effective_labels
}

output "expected_taints" {
  description = "Taints the validation should see, using Kubernetes effect spelling (mirrors kubectl spec.taints)."
  value       = var.taints
}

output "expected_instance_types" {
  description = "The machine type each node in the pool runs (GKE exposes node_config.machine_type, so this is populated, never [])."
  value       = [var.machine_type]
}

# Persist the cluster wiring resolved at CREATE into this pool's OWN state so a
# later DESTROY can read it back (terraform output -raw) and thread it as the
# fallback var — no dependency on the primary state's outputs surviving.
output "cluster_name" {
  description = "Primary cluster name this pool attached to (persisted for the destroy-time fallback)."
  value       = local.cluster_name
}

output "cluster_location" {
  description = "Primary cluster location this pool attached to (persisted for the destroy-time fallback)."
  value       = local.cluster_location
}
