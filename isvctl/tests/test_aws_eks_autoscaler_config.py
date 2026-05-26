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

"""Contract tests for AWS EKS Cluster Autoscaler provider wiring."""

from pathlib import Path

AWS_EKS_DIR = Path(__file__).resolve().parents[1] / "configs" / "providers" / "aws" / "scripts" / "eks"


def test_eks_terraform_installs_upstream_cluster_autoscaler_with_irsa() -> None:
    """EKS Terraform should install upstream Cluster Autoscaler with scoped IRSA."""
    terraform = (AWS_EKS_DIR / "terraform" / "main.tf").read_text(encoding="utf-8")

    assert 'module "cluster_autoscaler_irsa"' in terraform
    assert "attach_cluster_autoscaler_policy = true" in terraform
    assert "cluster_autoscaler_cluster_names = [local.cluster_name]" in terraform
    assert 'namespace_service_accounts = ["kube-system:cluster-autoscaler"]' in terraform

    assert 'resource "helm_release" "cluster_autoscaler"' in terraform
    assert 'repository = "https://kubernetes.github.io/autoscaler"' in terraform
    assert 'chart      = "cluster-autoscaler"' in terraform
    assert 'fullnameOverride = "cluster-autoscaler"' in terraform
    assert 'nameOverride     = "cluster-autoscaler"' in terraform
    assert '"scale-down-enabled"          = "false"' in terraform


def test_eks_setup_output_exposes_autoscaler_validation_metadata() -> None:
    """EKS setup output should tell the suite where to find Cluster Autoscaler."""
    setup_script = (AWS_EKS_DIR / "setup.sh").read_text(encoding="utf-8")

    assert '"cluster_autoscaler_namespace": "kube-system"' in setup_script
    assert '"cluster_autoscaler_deployment": "cluster-autoscaler"' in setup_script
