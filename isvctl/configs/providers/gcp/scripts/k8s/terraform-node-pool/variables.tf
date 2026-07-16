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

# Inputs for a single test node pool attached to the primary cluster. Threaded
# from create_node_pool.py as TF_VAR_* env exports (so a var-less
# `terraform destroy` inherits the same target-identifying values).

variable "project" {
  description = "GCP project ID."
  type        = string
}

variable "pool_name" {
  description = "RUN_ID-suffixed google_container_node_pool.name (create-time identity and scale/destroy target)."
  type        = string
  validation {
    condition     = length(var.pool_name) > 0 && length(var.pool_name) <= 40
    error_message = "pool_name must be 1..40 characters."
  }
}

variable "cluster_state_path" {
  description = "Relative path to the primary cluster's terraform state (read via terraform_remote_state for cluster wiring)."
  type        = string
  default     = "../terraform/terraform.tfstate"
}

variable "cluster_name" {
  description = <<-EOT
    Primary cluster name, threaded from the stub so it is persisted in THIS pool's
    own state (create reads it from the primary state; destroy reads it back from
    this module's own output). Used only as the DESTROY-time fallback when the
    primary state's outputs are already gone; at create the live
    terraform_remote_state value wins. Empty default keeps a manual apply working.
  EOT
  type        = string
  default     = ""
}

variable "cluster_location" {
  description = "Primary cluster location, persisted like cluster_name as the destroy-time fallback for terraform_remote_state.outputs.location."
  type        = string
  default     = ""
}

variable "node_count" {
  description = "Desired node count for the pool (min=max=this for a fixed pool). Bump on update to scale."
  type        = number
  default     = 1
  validation {
    condition     = var.node_count >= 0 && var.node_count <= 50 && floor(var.node_count) == var.node_count
    error_message = "node_count must be an integer in [0, 50]."
  }
}

variable "machine_type" {
  description = "node_config.machine_type for the pool."
  type        = string
}

variable "node_type" {
  description = "Informational pool kind: cpu | gpu."
  type        = string
  default     = "cpu"
  validation {
    condition     = contains(["cpu", "gpu"], var.node_type)
    error_message = "node_type must be cpu or gpu."
  }
}

variable "accelerator_type" {
  description = "node_config.guest_accelerator.type for a GPU pool (empty for a CPU pool)."
  type        = string
  default     = ""
}

variable "accelerator_count" {
  description = "node_config.guest_accelerator.count (GPUs per node) for a GPU pool."
  type        = number
  default     = 0
  validation {
    condition     = var.accelerator_count >= 0 && var.accelerator_count <= 16 && floor(var.accelerator_count) == var.accelerator_count
    error_message = "accelerator_count must be an integer in [0, 16]."
  }
}

variable "node_locations" {
  description = "Zone(s) the pool's nodes run in. For a GPU pool this is the single capacity-probed zone; empty -> the cluster's own node locations."
  type        = list(string)
  default     = []
}

variable "labels" {
  description = <<-EOT
    Extra Kubernetes labels to apply to every node in the pool. Merged on top of
    the stable markers (`isv.ncp.validation/pool=test`, `-pool-name`) this module
    always sets so kubectl probes can find the pool even with no caller labels.
  EOT
  type        = map(string)
  default     = {}
}

variable "taints" {
  description = <<-EOT
    Taints to apply to nodes in the pool. Effects use Kubernetes spelling
    (NoSchedule / PreferNoSchedule / NoExecute) so the same JSON payload feeds
    the validation, which compares against kubectl's spec.taints. Translated to
    the GKE enum on the way to the resource.
  EOT
  type = list(object({
    key    = string
    value  = optional(string, "")
    effect = string
  }))
  default = []
  validation {
    condition = alltrue([
      for t in var.taints : contains(["NoSchedule", "PreferNoSchedule", "NoExecute"], t.effect)
    ])
    error_message = "Each taint effect must be one of NoSchedule, PreferNoSchedule, NoExecute (Kubernetes spelling)."
  }
}
