#!/bin/bash
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

# Destroy the secondary EKS shared-VPC cluster created by
# create_shared_vpc_cluster.sh.
#
# Environment variables:
#   TF_AUTO_APPROVE               - "true" to skip confirmation (default: false)
#   SHARED_VPC_CLUSTER_STATE_FILE - Local Terraform state filename within
#                                   terraform-shared-vpc-cluster/ (default
#                                   "terraform.tfstate")

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform-shared-vpc-cluster"
STATE_FILE="${SHARED_VPC_CLUSTER_STATE_FILE:-terraform.tfstate}"

if [[ -z "${STATE_FILE}" ]] \
    || [[ "${STATE_FILE}" == .* ]] \
    || [[ "${STATE_FILE}" == *"/"* ]] \
    || [[ "${STATE_FILE}" == *".."* ]] \
    || [[ "${STATE_FILE}" == \~* ]] \
    || [[ ! "${STATE_FILE}" =~ ^[A-Za-z0-9._-]+\.tfstate$ ]]; then
    echo "Error: SHARED_VPC_CLUSTER_STATE_FILE must be a local .tfstate filename using only letters, numbers, dots, underscores, and hyphens." >&2
    exit 1
fi

if [ ! -f "${TF_DIR}/${STATE_FILE}" ]; then
    echo "No shared-VPC cluster state found at ${TF_DIR}/${STATE_FILE}; nothing to destroy." >&2
    cat << 'EOF'
{
  "success": true,
  "platform": "kubernetes",
  "message": "Shared-VPC cluster state absent - nothing to destroy",
  "resources_deleted": []
}
EOF
    exit 0
fi

if ! command -v terraform &> /dev/null; then
    echo "Error: terraform not found - install from https://terraform.io" >&2
    exit 1
fi

cd "${TF_DIR}"

echo "" >&2
echo "========================================" >&2
echo "  Destroying secondary EKS shared-VPC cluster" >&2
echo "  state file: ${STATE_FILE}" >&2
echo "========================================" >&2

if [ ! -d ".terraform" ]; then
    terraform init >&2
fi

TF_AUTO_APPROVE="${TF_AUTO_APPROVE:-false}"
if [ "${TF_AUTO_APPROVE}" = "true" ]; then
    terraform destroy -auto-approve -state="${STATE_FILE}" >&2
else
    terraform destroy -state="${STATE_FILE}" >&2
fi

cat << 'EOF'
{
  "success": true,
  "platform": "kubernetes",
  "message": "Secondary shared-VPC cluster destroyed",
  "resources_deleted": ["aws_eks_cluster", "aws_eks_node_group", "aws_ec2_tag"]
}
EOF
