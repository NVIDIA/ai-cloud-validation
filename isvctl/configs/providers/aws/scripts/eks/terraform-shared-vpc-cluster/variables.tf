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
  description = "AWS region. Leave empty to use the primary cluster state's region."
  type        = string
  default     = ""
}

variable "environment" {
  description = "Deployment environment tag, used for default tags."
  type        = string
  default     = "dev"
}

variable "cluster_name" {
  description = "Secondary EKS cluster name. Leave empty to derive from the primary cluster name."
  type        = string
  default     = ""
}

variable "kubernetes_version" {
  description = "Secondary cluster Kubernetes version. Leave empty to match the primary cluster."
  type        = string
  default     = ""
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "CIDR blocks allowed to access the secondary EKS API endpoint. Defaults to the primary cluster output."
  type        = list(string)
  default     = []
}

variable "node_group_name" {
  description = "Name of the secondary cluster's CPU-only managed node group."
  type        = string
  default     = "shared-vpc-cpu"
  validation {
    condition     = length(var.node_group_name) > 0 && length(var.node_group_name) <= 63
    error_message = "node_group_name must be 1..63 characters."
  }
}

variable "node_instance_types" {
  description = "CPU-only instance types for the secondary cluster managed node group."
  type        = list(string)
  default     = ["m6i.large"]
  validation {
    condition     = length(var.node_instance_types) > 0
    error_message = "node_instance_types must not be empty."
  }
}

variable "node_ami_type" {
  description = "EKS AMI type for the secondary CPU-only managed node group."
  type        = string
  default     = "AL2023_x86_64_STANDARD"
}

variable "node_capacity_type" {
  description = "Secondary node group capacity type: ON_DEMAND or SPOT."
  type        = string
  default     = "ON_DEMAND"
  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_capacity_type)
    error_message = "node_capacity_type must be ON_DEMAND or SPOT."
  }
}

variable "node_desired_size" {
  description = "CPU node count for the secondary cluster. min/max are pinned to this value."
  type        = number
  default     = 1
  validation {
    condition     = var.node_desired_size >= 1 && var.node_desired_size <= 50 && floor(var.node_desired_size) == var.node_desired_size
    error_message = "node_desired_size must be an integer in [1, 50]."
  }
}
