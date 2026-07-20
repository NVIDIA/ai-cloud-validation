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

# Inputs for the SECONDARY GKE cluster that coexists with the primary in the
# same VPC network (proves multi-cluster same-VPC). Threaded from
# create_test_shared_vpc_cluster.py as TF_VAR_* env exports.

variable "project" {
  description = "GCP project ID."
  type        = string
}

variable "cluster_name" {
  description = "RUN_ID-suffixed name of the secondary cluster."
  type        = string
  validation {
    condition     = length(var.cluster_name) > 0 && length(var.cluster_name) <= 40
    error_message = "cluster_name must be 1..40 characters."
  }
}

variable "cluster_state_path" {
  description = "Relative path to the primary cluster's terraform state (read to share its network)."
  type        = string
  default     = "../terraform/terraform.tfstate"
}

variable "location" {
  description = "Location for the secondary cluster (match the primary)."
  type        = string
}

variable "machine_type" {
  description = "node_config.machine_type for the secondary cluster's small node pool."
  type        = string
  default     = "e2-standard-4"
}

variable "ownership_labels" {
  description = <<-EOT
    Full-run-identity ownership marker stamped on the secondary cluster's
    resource_labels at CREATION (isv-ncp-run-id=<full run id>), matching the primary
    cluster. It is the adopt-safety proof cross-worker adopt and destroy require, so a
    same-name secondary this run does not own is never adopted or destroyed. Empty
    default keeps a var-less destroy valid.
  EOT
  type        = map(string)
  default     = {}
}

variable "node_count" {
  description = "Node count for the secondary cluster's node pool."
  type        = number
  default     = 1
}

variable "node_locations" {
  description = <<-EOT
    Single zone (as a one-element list) the secondary's node pool runs in, derived
    by the stub from the location so a REGIONAL secondary does not multiply
    node_count across every region zone (node_count is PER-ZONE). Empty -> inherit
    the cluster's node locations (a zonal cluster is already single-zone).
  EOT
  type        = list(string)
  default     = []
}

variable "network" {
  description = "Shared VPC network, threaded from the stub as the DESTROY-time fallback for terraform_remote_state.outputs.network (at create the live primary output wins)."
  type        = string
  default     = ""
}

variable "subnetwork" {
  description = "Shared subnetwork, threaded from the stub as the DESTROY-time fallback for terraform_remote_state.outputs.subnetwork."
  type        = string
  default     = ""
}
