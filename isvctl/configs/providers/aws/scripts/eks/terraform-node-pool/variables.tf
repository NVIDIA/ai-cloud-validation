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

variable "region" {
  description = "AWS region. Must match the cluster's region."
  type        = string
  default     = "us-west-2"
}

variable "environment" {
  description = "Deployment environment tag, used for default tags."
  type        = string
  default     = "dev"
}

variable "node_pool_name" {
  description = "Name of the EKS managed node group created by this module."
  type        = string
  default     = "isv-test-pool"
  validation {
    condition     = length(var.node_pool_name) > 0 && length(var.node_pool_name) <= 63
    error_message = "node_pool_name must be 1..63 characters."
  }
}

variable "instance_types" {
  description = <<-EOT
    Instance types to use for the test node group. The first available type
    in the cluster's AZs is chosen by EKS. Pass a single type (e.g.
    ["m6i.large"]) for a predictable shape, or multiple for broader AZ
    compatibility. For CPU-only high-performance-networking workloads use
    network-optimized types (e.g. "c5n.18xlarge", "c6in.32xlarge").
  EOT
  type        = list(string)
  default     = ["m6i.large"]
  validation {
    condition     = length(var.instance_types) > 0
    error_message = "instance_types must not be empty."
  }
}

variable "ami_type" {
  description = <<-EOT
    EKS AMI type. Use AL2023_x86_64_STANDARD or AL2023_ARM_64_STANDARD for
    CPU nodes, AL2_x86_64_GPU for legacy GPU nodes. See
    https://docs.aws.amazon.com/eks/latest/APIReference/API_Nodegroup.html
  EOT
  type        = string
  default     = "AL2023_x86_64_STANDARD"
}

variable "capacity_type" {
  description = "Node group capacity type: ON_DEMAND or SPOT."
  type        = string
  default     = "ON_DEMAND"
  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.capacity_type)
    error_message = "capacity_type must be ON_DEMAND or SPOT."
  }
}

variable "desired_size" {
  description = "Node count for the test pool. min/max are pinned to this value."
  type        = number
  default     = 1
  validation {
    condition     = var.desired_size >= 0 && var.desired_size <= 50 && floor(var.desired_size) == var.desired_size
    error_message = "desired_size must be an integer in [0, 50]."
  }
}

variable "labels" {
  description = <<-EOT
    Additional Kubernetes labels to apply to nodes in this pool. Merged on
    top of the stable markers (`isv.ncp.validation/pool` and `-pool-name`)
    that this module always sets.
  EOT
  type        = map(string)
  default     = {}
}

variable "taints" {
  description = <<-EOT
    Taints to apply to nodes in this pool. Each entry must provide `key` and
    `effect`; `value` defaults to empty string. Effects use Kubernetes
    spelling (``NoSchedule``, ``PreferNoSchedule``, ``NoExecute``) so the
    same JSON payload can be forwarded directly to the validation, which
    compares against ``kubectl get node -o json``. The module translates
    to the EKS enum spelling internally.
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
