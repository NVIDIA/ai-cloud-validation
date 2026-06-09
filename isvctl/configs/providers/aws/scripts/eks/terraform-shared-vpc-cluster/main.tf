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

# Secondary EKS Cluster in the Primary Cluster's VPC
#
# This module reads the primary cluster's local Terraform state from
# ../terraform/terraform.tfstate, reuses its VPC/private subnets, and creates a
# second EKS cluster with one CPU-only managed node group. The module has its
# own local state so the shared-VPC proof can be created and destroyed before
# the primary cluster teardown.

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

data "terraform_remote_state" "cluster" {
  backend = "local"
  config = {
    path = "../terraform/terraform.tfstate"
  }
}

data "aws_caller_identity" "current" {}

locals {
  primary_cluster_name = data.terraform_remote_state.cluster.outputs.cluster_name
  region               = var.region != "" ? var.region : data.terraform_remote_state.cluster.outputs.region
  kubernetes_version = (
    var.kubernetes_version != ""
    ? var.kubernetes_version
    : data.terraform_remote_state.cluster.outputs.cluster_version
  )
  cluster_name = (
    var.cluster_name != ""
    ? var.cluster_name
    : substr("${local.primary_cluster_name}-shared-vpc", 0, 63)
  )
  endpoint_public_access_cidrs = (
    length(var.cluster_endpoint_public_access_cidrs) > 0
    ? var.cluster_endpoint_public_access_cidrs
    : try(data.terraform_remote_state.cluster.outputs.cluster_endpoint_public_access_cidrs, ["203.0.113.0/24"])
  )
  vpc_id          = data.terraform_remote_state.cluster.outputs.vpc_id
  private_subnets = data.terraform_remote_state.cluster.outputs.private_subnets

  tags = {
    ClusterName    = local.cluster_name
    Environment    = var.environment
    PrimaryCluster = local.primary_cluster_name
    TestID         = "K8S26-01"
  }
}

provider "aws" {
  region = local.region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = "isv-lab-tools"
      ManagedBy   = "terraform"
      Component   = "shared-vpc-cluster"
      TestID      = "K8S26-01"
    }
  }
}

resource "aws_ec2_tag" "secondary_cluster_private_subnets" {
  for_each = toset(local.private_subnets)

  resource_id = each.value
  key         = "kubernetes.io/cluster/${local.cluster_name}"
  value       = "shared"
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.cluster_name
  cluster_version = local.kubernetes_version

  cluster_endpoint_public_access       = true
  cluster_endpoint_private_access      = true
  cluster_endpoint_public_access_cidrs = local.endpoint_public_access_cidrs

  bootstrap_self_managed_addons = false

  vpc_id     = local.vpc_id
  subnet_ids = local.private_subnets

  control_plane_subnet_ids = local.private_subnets

  enable_cluster_creator_admin_permissions = true

  node_security_group_additional_rules = {
    ingress_self_all = {
      description = "Node to node all ports/protocols"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "ingress"
      self        = true
    }
    ingress_cluster_all = {
      description                   = "Cluster to node all ports/protocols"
      protocol                      = "-1"
      from_port                     = 0
      to_port                       = 0
      type                          = "ingress"
      source_cluster_security_group = true
    }
    egress_all = {
      description = "Node all egress"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "egress"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  cluster_addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      most_recent    = true
      before_compute = true
      configuration_values = jsonencode({
        env = {
          ENABLE_PREFIX_DELEGATION = "true"
          WARM_PREFIX_TARGET       = "1"
        }
      })
    }
  }

  eks_managed_node_groups = {
    secondary_cpu = {
      name           = var.node_group_name
      instance_types = var.node_instance_types
      ami_type       = var.node_ami_type
      capacity_type  = var.node_capacity_type

      min_size     = var.node_desired_size
      max_size     = var.node_desired_size
      desired_size = var.node_desired_size

      subnet_ids = local.private_subnets

      labels = {
        role                              = "system"
        "isv.ncp.validation/cluster-role" = "secondary"
      }

      iam_role_additional_policies = {
        AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
      }
    }
  }

  tags = local.tags

  depends_on = [aws_ec2_tag.secondary_cluster_private_subnets]
}
