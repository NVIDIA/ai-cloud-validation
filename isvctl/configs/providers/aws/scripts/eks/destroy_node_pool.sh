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

# Destroy the test node pool created by create_node_pool.sh.
#
# Runs `terraform destroy` on the isolated terraform-node-pool state. This
# runs before the cluster teardown so the node group's ENIs and instances
# are freed before the VPC comes down. NODE_POOL_STATE_FILE must match the
# value used at create time so the correct pool is torn down (the create and
# destroy steps for a given pool share one state file).
#
# Environment variables:
#   TF_AUTO_APPROVE      - "true" to skip confirmation (default: false)
#   NODE_POOL_STATE_FILE - Local Terraform state filename within
#                          terraform-node-pool/ (default "terraform.tfstate").

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform-node-pool"
STATE_FILE="${NODE_POOL_STATE_FILE:-terraform.tfstate}"

if [[ -z "${STATE_FILE}" ]] \
    || [[ "${STATE_FILE}" == .* ]] \
    || [[ "${STATE_FILE}" == *"/"* ]] \
    || [[ "${STATE_FILE}" == *".."* ]] \
    || [[ "${STATE_FILE}" == \~* ]] \
    || [[ ! "${STATE_FILE}" =~ ^[A-Za-z0-9._-]+\.tfstate$ ]]; then
    echo "Error: NODE_POOL_STATE_FILE must be a local .tfstate filename using only letters, numbers, dots, underscores, and hyphens." >&2
    exit 1
fi

if ! command -v terraform &> /dev/null; then
    echo "Error: terraform not found" >&2
    exit 1
fi

# If the module was never applied (e.g. create step failed before
# `terraform apply`), there is no state to destroy. Report success so the
# overall teardown phase can proceed to the main cluster destroy.
if [ ! -f "${TF_DIR}/${STATE_FILE}" ]; then
    echo "No node-pool state found at ${TF_DIR}/${STATE_FILE}; nothing to destroy." >&2
    cat << 'EOF'
{
  "success": true,
  "platform": "kubernetes",
  "message": "Node pool state absent - nothing to destroy",
  "resources_deleted": []
}
EOF
    exit 0
fi

cd "${TF_DIR}"

echo "" >&2
echo "========================================" >&2
echo "  Destroying test node pool" >&2
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
  "message": "Test node pool destroyed",
  "resources_deleted": ["aws_eks_node_group", "aws_iam_role"]
}
EOF
