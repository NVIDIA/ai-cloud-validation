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

"""Contract tests for the default AWS EKS version and GPU node wiring."""

from pathlib import Path

AWS_EKS_DIR = Path(__file__).resolve().parents[3] / "configs" / "providers" / "aws"


def test_eks_default_version_has_current_gpu_ami_support() -> None:
    """The default supported minor must not use the retired AL2 GPU AMI."""
    variables = (AWS_EKS_DIR / "scripts" / "eks" / "terraform" / "variables.tf").read_text(encoding="utf-8")
    terraform = (AWS_EKS_DIR / "scripts" / "eks" / "terraform" / "main.tf").read_text(encoding="utf-8")
    example = (AWS_EKS_DIR / "scripts" / "eks" / "terraform" / "terraform.tfvars.example").read_text(encoding="utf-8")

    assert 'default     = "1.35"' in variables
    assert 'kubernetes_version = "1.35"' in example
    assert 'ami_type = "AL2023_x86_64_NVIDIA"' in terraform
    assert 'ami_type       = "AL2_x86_64_GPU"' not in terraform


def test_eks_gpu_operator_uses_drivers_from_accelerated_ami() -> None:
    """GPU Operator must not reinstall components supplied by the EKS AMI."""
    terraform = (AWS_EKS_DIR / "scripts" / "eks" / "terraform" / "main.tf").read_text(encoding="utf-8")

    assert "driver = {\n      # The EKS AL2023 NVIDIA AMI supplies the host driver.\n      enabled = false" in terraform
    assert (
        "toolkit = {\n      # The EKS AL2023 NVIDIA AMI supplies the container toolkit.\n      enabled = false"
        in terraform
    )


def test_eks_autoscaler_tag_remains_derived_from_cluster_version() -> None:
    """Leaving the image override empty should derive the matching tag."""
    terraform = (AWS_EKS_DIR / "scripts" / "eks" / "terraform" / "main.tf").read_text(encoding="utf-8")

    assert ': "v${var.kubernetes_version}.0"' in terraform
