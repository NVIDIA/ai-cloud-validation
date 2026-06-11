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

# AWS EKS GPU Cluster Terraform Configuration
# This module deploys an EKS cluster with GPU nodes for ISV Lab validation testing.
#
# Usage:
#   cd isvctl/configs/providers/aws/scripts/eks/terraform
#   terraform init
#   terraform plan
#   terraform apply
#
# After deployment:
#   aws eks update-kubeconfig --name $(terraform output -raw cluster_name) --region $(terraform output -raw region)
#   isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.20"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.10"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
  }

  # Local backend - state saved in current directory
  backend "local" {
    path = "terraform.tfstate"
  }
}

# -----------------------------------------------------------------------------
# Providers
# -----------------------------------------------------------------------------

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = "isv-lab-tools"
      ManagedBy   = "terraform"
    }
  }
}

# Kubernetes provider configured after EKS cluster is created
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}

# -----------------------------------------------------------------------------
# Data Sources
# -----------------------------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

# Get AZs that support the GPU instance type
# GPU instances (g4dn, g5, p4d, p5) are only available in specific AZs
data "aws_ec2_instance_type_offerings" "gpu" {
  filter {
    name   = "instance-type"
    values = var.gpu_node_instance_types
  }

  filter {
    name   = "location"
    values = data.aws_availability_zones.available.names
  }

  location_type = "availability-zone"
}

# -----------------------------------------------------------------------------
# Local Values
# -----------------------------------------------------------------------------

locals {
  cluster_name = "${var.cluster_name_prefix}-${var.environment}"
  cluster_autoscaler_image_tag = (
    var.cluster_autoscaler_image_tag != ""
    ? var.cluster_autoscaler_image_tag
    : "v${var.kubernetes_version}.0"
  )

  # Filter AZs to only those that support the GPU instance type
  # This prevents NodeCreationFailure when EKS tries to launch GPU nodes in unsupported AZs
  gpu_supported_azs = toset(data.aws_ec2_instance_type_offerings.gpu.locations)

  # Use AZs that support GPU instances, limited to 3
  # Fall back to first 3 available AZs if no GPU AZs found (e.g., GPU node group disabled)
  azs = length(local.gpu_supported_azs) > 0 ? slice(
    sort(tolist(local.gpu_supported_azs)),
    0,
    min(3, length(local.gpu_supported_azs))
    ) : slice(
    data.aws_availability_zones.available.names,
    0,
    min(3, length(data.aws_availability_zones.available.names))
  )

  tags = {
    ClusterName = local.cluster_name
    Environment = var.environment
  }
}

# -----------------------------------------------------------------------------
# VPC Module
# -----------------------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${local.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 4, i + 4)]

  enable_nat_gateway   = true
  single_nat_gateway   = var.single_nat_gateway
  enable_dns_hostnames = true
  enable_dns_support   = true

  # Required for EKS
  public_subnet_tags = {
    "kubernetes.io/role/elb"                      = 1
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"             = 1
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
  }

  tags = local.tags
}

# -----------------------------------------------------------------------------
# VPC Endpoints for EKS in Private Subnets
# These allow nodes in private subnets to reach AWS services
# -----------------------------------------------------------------------------

# Security group for VPC endpoints
resource "aws_security_group" "vpc_endpoints" {
  name_prefix = "${local.cluster_name}-vpc-endpoints-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-vpc-endpoints"
  })
}

# ECR API endpoint (for pulling container images)
resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-ecr-api"
  })
}

# ECR DKR endpoint (for Docker registry)
resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-ecr-dkr"
  })
}

# S3 Gateway endpoint (for ECR image layers and other S3 access)
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.private_route_table_ids

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-s3"
  })
}

# STS endpoint (for IAM role assumption, required for IRSA)
resource "aws_vpc_endpoint" "sts" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.sts"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-sts"
  })
}

# EC2 endpoint (for EC2 API calls from nodes)
resource "aws_vpc_endpoint" "ec2" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ec2"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-ec2"
  })
}

# -----------------------------------------------------------------------------
# EKS Cluster
# -----------------------------------------------------------------------------

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.cluster_name
  cluster_version = var.kubernetes_version

  cluster_endpoint_public_access       = true
  cluster_endpoint_private_access      = true
  cluster_endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs

  # Prevent cluster replacement on import
  bootstrap_self_managed_addons = false

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Control plane subnets (use private subnets for ENIs)
  control_plane_subnet_ids = module.vpc.private_subnets

  # Cluster access configuration
  enable_cluster_creator_admin_permissions = true

  # Security group rules for node-to-control-plane communication
  # This is critical for nodes to join the cluster
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

  # EKS Addons - vpc-cni is critical for node networking
  cluster_addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      most_recent    = true
      before_compute = true # Ensure VPC CNI is ready before nodes join
      configuration_values = jsonencode({
        env = {
          ENABLE_PREFIX_DELEGATION = "true"
          WARM_PREFIX_TARGET       = "1"
        }
      })
    }
    aws-ebs-csi-driver = {
      most_recent              = true
      service_account_role_arn = module.ebs_csi_irsa.iam_role_arn
    }
  }

  # Node groups
  eks_managed_node_groups = {
    # System node group (non-GPU)
    system = {
      name           = "system"
      instance_types = var.system_node_instance_types
      ami_type       = "AL2023_x86_64_STANDARD"

      min_size     = var.system_node_min_size
      max_size     = var.system_node_max_size
      desired_size = var.system_node_desired_size

      # Ensure nodes can reach the internet via NAT gateway
      subnet_ids = module.vpc.private_subnets

      labels = {
        role = "system"
      }

      # IAM role for node group
      iam_role_additional_policies = {
        AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
      }
    }

    # GPU node group
    gpu = {
      name           = "gpu"
      instance_types = var.gpu_node_instance_types
      ami_type       = "AL2_x86_64_GPU"

      min_size     = var.gpu_node_min_size
      max_size     = var.gpu_node_max_size
      desired_size = var.gpu_node_desired_size

      # Use VPC private subnets - already built using local.azs which is GPU-constrained
      # The VPC's AZs are already filtered by local.gpu_supported_azs when available
      subnet_ids = module.vpc.private_subnets

      # GPU instances often need larger root volumes
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = var.gpu_node_volume_size
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }

      labels = {
        role                      = "gpu"
        "nvidia.com/gpu.workload" = "true"
      }

      taints = var.gpu_node_taints ? [{
        key    = "nvidia.com/gpu"
        value  = "true"
        effect = "NO_SCHEDULE"
      }] : []

      # IAM role for node group
      iam_role_additional_policies = {
        AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
      }
    }
  }

  tags = local.tags
}

# -----------------------------------------------------------------------------
# IRSA for EBS CSI Driver
# -----------------------------------------------------------------------------

module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name             = "${local.cluster_name}-ebs-csi"
  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }

  tags = local.tags
}

# -----------------------------------------------------------------------------
# Cluster Autoscaler IRSA
# -----------------------------------------------------------------------------

module "cluster_autoscaler_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"
  count   = var.install_cluster_autoscaler ? 1 : 0

  role_name                        = "${local.cluster_name}-cluster-autoscaler"
  attach_cluster_autoscaler_policy = true
  cluster_autoscaler_cluster_names = [local.cluster_name]

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:cluster-autoscaler"]
    }
  }

  tags = local.tags
}

# -----------------------------------------------------------------------------
# EFS for ReadWriteMany Storage (NIM model cache)
# -----------------------------------------------------------------------------

resource "aws_efs_file_system" "nim_cache" {
  count = var.enable_efs ? 1 : 0

  creation_token = "${local.cluster_name}-nim-cache"
  encrypted      = true

  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-nim-cache"
  })
}

resource "aws_efs_mount_target" "nim_cache" {
  count = var.enable_efs ? length(module.vpc.private_subnets) : 0

  file_system_id  = aws_efs_file_system.nim_cache[0].id
  subnet_id       = module.vpc.private_subnets[count.index]
  security_groups = [aws_security_group.efs[0].id]
}

resource "aws_security_group" "efs" {
  count = var.enable_efs ? 1 : 0

  name_prefix = "${local.cluster_name}-efs-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-efs"
  })
}

# IRSA for EFS CSI Driver
module "efs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"
  count   = var.enable_efs ? 1 : 0

  role_name             = "${local.cluster_name}-efs-csi"
  attach_efs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:efs-csi-controller-sa"]
    }
  }

  tags = local.tags
}

# -----------------------------------------------------------------------------
# GPU Operator (Helm)
# -----------------------------------------------------------------------------

resource "helm_release" "gpu_operator" {
  count = var.install_gpu_operator ? 1 : 0

  name             = "gpu-operator"
  repository       = "https://helm.ngc.nvidia.com/nvidia"
  chart            = "gpu-operator"
  version          = var.gpu_operator_version
  namespace        = "gpu-operator"
  create_namespace = true

  # Wait for GPU nodes to be ready
  depends_on = [module.eks]

  # GPU Operator configuration
  values = [yamlencode({
    driver = {
      enabled = true
    }
    toolkit = {
      enabled = true
    }
    devicePlugin = {
      enabled = true
    }
    mig = {
      strategy = var.mig_strategy
    }
    daemonsets = var.gpu_node_taints ? {
      tolerations = [
        {
          key      = "nvidia.com/gpu"
          operator = "Exists"
          effect   = "NoSchedule"
        }
      ]
    } : {}
  })]

  timeout = 900 # 15 minutes - GPU operator can take a while
}

# -----------------------------------------------------------------------------
# Cluster Autoscaler (Helm)
# -----------------------------------------------------------------------------

resource "helm_release" "cluster_autoscaler" {
  count = var.install_cluster_autoscaler ? 1 : 0

  name       = "cluster-autoscaler"
  repository = "https://kubernetes.github.io/autoscaler"
  chart      = "cluster-autoscaler"
  version    = var.cluster_autoscaler_chart_version != "" ? var.cluster_autoscaler_chart_version : null
  namespace  = "kube-system"

  depends_on = [
    module.eks,
    module.cluster_autoscaler_irsa,
  ]

  values = [yamlencode({
    fullnameOverride = "cluster-autoscaler"
    nameOverride     = "cluster-autoscaler"
    cloudProvider    = "aws"
    awsRegion        = var.region
    autoDiscovery = {
      clusterName = local.cluster_name
    }
    image = {
      tag = local.cluster_autoscaler_image_tag
    }
    replicaCount = 1
    rbac = {
      serviceAccount = {
        create = true
        name   = "cluster-autoscaler"
        annotations = {
          "eks.amazonaws.com/role-arn" = module.cluster_autoscaler_irsa[0].iam_role_arn
        }
      }
    }
    extraArgs = {
      "balance-similar-node-groups" = "true"
      "scale-down-enabled"          = "false"
      "skip-nodes-with-system-pods" = "true"
    }
  })]

  timeout = 300
}

# -----------------------------------------------------------------------------
# EFS CSI Driver (Helm)
# -----------------------------------------------------------------------------

resource "helm_release" "efs_csi_driver" {
  count = var.enable_efs ? 1 : 0

  name       = "aws-efs-csi-driver"
  repository = "https://kubernetes-sigs.github.io/aws-efs-csi-driver/"
  chart      = "aws-efs-csi-driver"
  namespace  = "kube-system"

  depends_on = [module.eks]

  # EFS CSI Driver configuration
  values = [yamlencode({
    controller = {
      serviceAccount = {
        create = true
        name   = "efs-csi-controller-sa"
        annotations = {
          "eks.amazonaws.com/role-arn" = module.efs_csi_irsa[0].iam_role_arn
        }
      }
    }
  })]
}

# EFS StorageClass
resource "kubernetes_storage_class_v1" "efs" {
  count = var.enable_efs ? 1 : 0

  metadata {
    name = "efs-sc"
  }

  storage_provisioner = "efs.csi.aws.com"
  reclaim_policy      = "Delete"

  parameters = {
    provisioningMode = "efs-ap"
    fileSystemId     = aws_efs_file_system.nim_cache[0].id
    directoryPerms   = "700"
  }

  depends_on = [helm_release.efs_csi_driver]
}

# -----------------------------------------------------------------------------
# Default gp3 StorageClass
# -----------------------------------------------------------------------------

resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Delete"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type      = "gp3"
    encrypted = "true"
  }

  depends_on = [module.eks]
}

# Remove default annotation from gp2 if it exists
resource "kubernetes_annotations" "gp2_not_default" {
  api_version = "storage.k8s.io/v1"
  kind        = "StorageClass"
  metadata {
    name = "gp2"
  }
  annotations = {
    "storageclass.kubernetes.io/is-default-class" = "false"
  }

  depends_on = [module.eks]
}
