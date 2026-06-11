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

# Node Pool Create via Terraform (AWS EKS)
#
# This module attaches a new EKS-managed node group to the cluster already
# provisioned by ../terraform, exercising the "create node pool via Terraform
# provider" leg of the node-pool CRUD requirement. It is run by
# create_node_pool.sh as part of the EKS validation setup phase;
# destroy_node_pool.sh tears it down.
#
# The module has its own local state (backend "local" -> terraform.tfstate in
# this directory) so the round-trip is isolated from the main cluster state.
# Cluster wiring (name, subnets) is read from the main cluster's state via
# terraform_remote_state.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = "isv-lab-tools"
      ManagedBy   = "terraform"
      Component   = "test-node-pool"
    }
  }
}

# Read cluster wiring from the main cluster module's state. The path is
# relative to this directory.
data "terraform_remote_state" "cluster" {
  backend = "local"
  config = {
    path = "../terraform/terraform.tfstate"
  }
}

data "aws_eks_cluster" "this" {
  name = data.terraform_remote_state.cluster.outputs.cluster_name
}

locals {
  cluster_name    = data.terraform_remote_state.cluster.outputs.cluster_name
  private_subnets = data.terraform_remote_state.cluster.outputs.private_subnets

  # Labels applied to the test pool. Callers override via TF_VAR_test_pool_labels_json,
  # but every node is also tagged with a stable marker so kubectl probes can
  # identify them even if the caller omits labels entirely.
  effective_labels = merge(
    {
      "isv.ncp.validation/pool"      = "test"
      "isv.ncp.validation/pool-name" = var.node_pool_name
    },
    var.labels,
  )

  # Kubernetes uses "NoSchedule"; the EKS API expects "NO_SCHEDULE". The
  # variable accepts the Kubernetes spelling so the same JSON payload can be
  # shared with the validation; translate here on the way to the resource.
  effect_to_eks = {
    NoSchedule       = "NO_SCHEDULE"
    PreferNoSchedule = "PREFER_NO_SCHEDULE"
    NoExecute        = "NO_EXECUTE"
  }
}

# -----------------------------------------------------------------------------
# IAM role for the test node group
# -----------------------------------------------------------------------------
# The test pool gets its own role rather than reusing the cluster module's
# role so this module can be applied/destroyed without mutating the main
# cluster's IAM resources. The role matches the EKS worker-node baseline.

resource "aws_iam_role" "node" {
  name_prefix = "${local.cluster_name}-${var.node_pool_name}-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "worker_node" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# -----------------------------------------------------------------------------
# The node group itself
# -----------------------------------------------------------------------------

resource "aws_eks_node_group" "this" {
  cluster_name    = local.cluster_name
  node_group_name = var.node_pool_name
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = local.private_subnets

  instance_types = var.instance_types
  ami_type       = var.ami_type
  capacity_type  = var.capacity_type

  scaling_config {
    desired_size = var.desired_size
    min_size     = var.desired_size
    max_size     = var.desired_size
  }

  labels = local.effective_labels

  dynamic "taint" {
    for_each = var.taints
    content {
      key    = taint.value.key
      value  = lookup(taint.value, "value", "")
      effect = local.effect_to_eks[taint.value.effect]
    }
  }

  # Policy attachments must exist before the node group comes up, else the
  # kubelet on joining nodes won't be able to reach the API server or pull
  # images.
  depends_on = [
    aws_iam_role_policy_attachment.worker_node,
    aws_iam_role_policy_attachment.cni,
    aws_iam_role_policy_attachment.ecr_read,
  ]
}
