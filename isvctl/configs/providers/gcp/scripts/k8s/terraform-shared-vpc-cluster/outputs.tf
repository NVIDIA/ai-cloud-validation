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

output "cluster_name" {
  description = "The secondary cluster name (destroy target)."
  value       = google_container_cluster.secondary.name
}

output "location" {
  description = "Secondary cluster location."
  value       = google_container_cluster.secondary.location
}

output "network" {
  description = "Shared VPC network the secondary attached to (equals the primary's). Persisted so a destroy can read it back as the network fallback var."
  value       = google_container_cluster.secondary.network
}

output "subnetwork" {
  description = "Shared subnetwork the secondary attached to. Persisted so a destroy can read it back as the subnetwork fallback var (no dependency on the primary state surviving)."
  value       = google_container_cluster.secondary.subnetwork
}
