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

# Inputs for the primary GKE cluster module. Every value is threaded from the
# setup.py stub as a TF_VAR_* ENVIRONMENT export (not one-shot -var on apply)
# so a var-less `terraform destroy` inherits the identical set and never aborts
# with "No value for required variable". Target-identifying inputs
# (project, cluster_name, location, gpu_node_locations) MUST resolve to the
# SAME run-scoped values at destroy time as at create time.

variable "project" {
  description = "GCP project ID (resolved by the stub via resolve_project -> ADC)."
  type        = string
}

variable "cluster_name" {
  description = "RUN_ID-suffixed google_container_cluster.name (URL-keyed identity AND destroy target)."
  type        = string
  validation {
    condition     = length(var.cluster_name) > 0 && length(var.cluster_name) <= 40
    error_message = "cluster_name must be 1..40 characters (GKE RFC 1035 cluster-name cap)."
  }
}

variable "location" {
  description = "google_container_cluster.location (region or zone)."
  type        = string
}

variable "kube_version" {
  description = "min_master_version pin; empty string -> the REGULAR release channel default."
  type        = string
  default     = ""
}

variable "network" {
  description = "VPC network the cluster attaches to."
  type        = string
  default     = "default"
}

variable "subnetwork" {
  description = "Subnetwork the cluster attaches to (empty -> GKE picks the network's default subnet in the region)."
  type        = string
  default     = ""
}

variable "master_authorized_cidrs" {
  description = <<-EOT
    Operator-approved CIDRs allowed to reach the GKE control-plane public
    endpoint (GKE authorized networks). Empty list -> the
    master_authorized_networks_config block is omitted (endpoint open). The stub
    validates every entry as a non-world-open CIDR before threading it here, so a
    var-less destroy defaults it safely.
  EOT
  type        = list(string)
  default     = []
}

variable "system_machine_type" {
  description = "node_config.machine_type for the small system (CPU) node pool."
  type        = string
}

variable "system_node_count" {
  description = "Node count for the system pool."
  type        = number
  default     = 1
  validation {
    condition     = var.system_node_count >= 1 && var.system_node_count <= 10 && floor(var.system_node_count) == var.system_node_count
    error_message = "system_node_count must be an integer in [1, 10]."
  }
}

variable "system_min_nodes" {
  description = "Lower bound (per zone) for the GKE-managed autoscaler on the system pool."
  type        = number
  default     = 1
  validation {
    condition     = var.system_min_nodes >= 1 && var.system_min_nodes <= 10 && floor(var.system_min_nodes) == var.system_min_nodes
    error_message = "system_min_nodes must be an integer in [1, 10]."
  }
}

variable "system_max_nodes" {
  description = "Upper bound (per zone) for the GKE-managed autoscaler on the system pool (>= system_min_nodes)."
  type        = number
  default     = 3
  validation {
    condition     = var.system_max_nodes >= var.system_min_nodes && var.system_max_nodes <= 20 && floor(var.system_max_nodes) == var.system_max_nodes
    error_message = "system_max_nodes must be an integer in [system_min_nodes, 20]."
  }
}

variable "system_node_locations" {
  description = <<-EOT
    Single zone (as a one-element list) the system pool's nodes run in, derived by
    the stub from the cluster location so a REGIONAL cluster does not multiply
    system node_count across every region zone (node_count is PER-ZONE). Empty ->
    the pool inherits the cluster's node locations (a zonal cluster is already
    single-zone). Delete-irrelevant, so a var-less destroy defaults it safely.
  EOT
  type        = list(string)
  default     = []
}

variable "gpu_machine_type" {
  description = "node_config.machine_type for the baseline GPU node pool (a GPU-capable machine)."
  type        = string
}

variable "gpu_accelerator_type" {
  description = "node_config.guest_accelerator.type for the baseline GPU pool (e.g. nvidia-l4)."
  type        = string
}

variable "gpu_accelerator_count" {
  description = "node_config.guest_accelerator.count (GPUs per node)."
  type        = number
  default     = 1
  validation {
    condition     = var.gpu_accelerator_count >= 1 && var.gpu_accelerator_count <= 16 && floor(var.gpu_accelerator_count) == var.gpu_accelerator_count
    error_message = "gpu_accelerator_count must be an integer in [1, 16]."
  }
}

variable "gpu_node_count" {
  description = "Node count for the baseline GPU pool (fixed, single-zone)."
  type        = number
  default     = 1
  validation {
    condition     = var.gpu_node_count >= 1 && var.gpu_node_count <= 20 && floor(var.gpu_node_count) == var.gpu_node_count
    error_message = "gpu_node_count must be an integer in [1, 20]."
  }
}

variable "gpu_node_locations" {
  description = <<-EOT
    Single zone (as a one-element list) for the baseline GPU pool. The stub
    PICKS this zone with a standalone-MIG capacity preflight BEFORE apply — a
    GKE node-pool CREATE op cannot be cancelled, so the pool is never created
    speculatively in an unprobed zone.
  EOT
  type        = list(string)
  validation {
    condition     = length(var.gpu_node_locations) == 1
    error_message = "gpu_node_locations must contain exactly one capacity-probed zone."
  }
}
