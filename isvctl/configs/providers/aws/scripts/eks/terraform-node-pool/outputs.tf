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

# Outputs consumed by create_node_pool.sh to build the node_pool JSON payload
# that the setup step emits; the validation reads these via
# {{steps.create_test_node_pool.*}} in the suite config.

output "node_pool_name" {
  description = "Name of the created EKS managed node group."
  value       = aws_eks_node_group.this.node_group_name
}

output "label_selector" {
  description = <<-EOT
    kubectl label selector identifying nodes in this pool. EKS always labels
    managed-nodegroup nodes with eks.amazonaws.com/nodegroup=<name>, so that
    is what the validation polls on.
  EOT
  value       = "eks.amazonaws.com/nodegroup=${aws_eks_node_group.this.node_group_name}"
}

output "desired_size" {
  description = "Configured node count for the pool (min=max=desired)."
  value       = aws_eks_node_group.this.scaling_config[0].desired_size
}

output "expected_labels" {
  description = "Labels the validation should see on every node (stable markers + caller-supplied)."
  value       = local.effective_labels
}

output "expected_taints" {
  description = <<-EOT
    Taints the validation should see on every node, using Kubernetes effect
    spelling (NoSchedule/PreferNoSchedule/NoExecute). These mirror what
    kubectl reports under `spec.taints`, not the EKS enum values.
  EOT
  value       = var.taints
}

output "expected_instance_types" {
  description = "Instance types accepted for each node in the pool."
  value       = var.instance_types
}
