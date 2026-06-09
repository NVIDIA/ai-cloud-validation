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
  description = "Secondary EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_arn" {
  description = "Secondary EKS cluster ARN."
  value       = module.eks.cluster_arn
}

output "cluster_endpoint" {
  description = "Secondary EKS API endpoint."
  value       = module.eks.cluster_endpoint
}

output "cluster_version" {
  description = "Secondary EKS Kubernetes version."
  value       = module.eks.cluster_version
}

output "region" {
  description = "AWS region."
  value       = local.region
}

output "tenancy_id" {
  description = "AWS account ID."
  value       = data.aws_caller_identity.current.account_id
}

output "vpc_id" {
  description = "Shared VPC ID reused from the primary cluster."
  value       = local.vpc_id
}

output "private_subnets" {
  description = "Private subnet IDs reused from the primary cluster."
  value       = local.private_subnets
}

output "node_group_name" {
  description = "Secondary cluster CPU node group name."
  value       = var.node_group_name
}
