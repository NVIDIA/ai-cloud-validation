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

# Outputs read back by setup.py (for the kubeconfig install / inventory) and by
# the node-pool + shared-VPC modules via terraform_remote_state.

output "cluster_name" {
  description = "The RUN_ID-suffixed cluster name (create-time identity and destroy target)."
  value       = google_container_cluster.primary.name
}

output "location" {
  description = "Cluster location (region or zone)."
  value       = google_container_cluster.primary.location
}

output "project" {
  description = "GCP project the cluster lives in."
  value       = var.project
}

output "network" {
  description = "VPC network the cluster attaches to (read by the shared-VPC module to prove same-VPC coexistence)."
  value       = google_container_cluster.primary.network
}

output "subnetwork" {
  description = "Subnetwork the cluster attaches to."
  value       = google_container_cluster.primary.subnetwork
}

output "gpu_zone" {
  description = "The capacity-probed zone the baseline GPU pool landed in."
  value       = var.gpu_node_locations[0]
}
