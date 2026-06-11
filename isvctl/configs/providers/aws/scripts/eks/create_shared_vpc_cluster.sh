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

# Create a secondary EKS cluster in the primary cluster's VPC.
#
# Applies ./terraform-shared-vpc-cluster/ against the existing primary cluster
# state and emits a JSON payload matching the `multi_cluster` output schema.
# The validation consumes only this JSON, so AWS-specific API calls remain in
# this provider script.
#
# Environment variables:
#   TF_AUTO_APPROVE                    - "true" to skip approval (default: false)
#   SHARED_VPC_CLUSTER_STATE_FILE      - Local Terraform state filename within
#                                        terraform-shared-vpc-cluster/ (default
#                                        "terraform.tfstate")
#   SECONDARY_CLUSTER_READY_TIMEOUT    - Seconds to wait for a Ready node
#                                        (default 900)
#   SECONDARY_CLUSTER_POLL_INTERVAL    - Poll interval in seconds (default 10)
#   TF_VAR_*                           - Terraform variables for the secondary
#                                        cluster module

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform-shared-vpc-cluster"
CLUSTER_TF_DIR="${SCRIPT_DIR}/terraform"
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

if ! command -v terraform &> /dev/null; then
    echo "Error: terraform not found - install from https://terraform.io" >&2
    exit 1
fi
if ! command -v aws &> /dev/null; then
    echo "Error: AWS CLI not found" >&2
    exit 1
fi
if ! command -v kubectl &> /dev/null; then
    echo "Error: kubectl not found" >&2
    exit 1
fi
if ! command -v jq &> /dev/null; then
    echo "Error: jq not found" >&2
    exit 1
fi
if [ ! -f "${CLUSTER_TF_DIR}/terraform.tfstate" ]; then
    echo "Error: primary cluster state not found at ${CLUSTER_TF_DIR}/terraform.tfstate" >&2
    echo "Run the EKS setup step first." >&2
    exit 1
fi
if ! aws sts get-caller-identity &> /dev/null 2>&1; then
    echo "Error: AWS credentials not configured" >&2
    exit 1
fi

ready_nodes_for_cluster() {
    local cluster_name="$1"
    local region="$2"
    local kubeconfig_path="$3"

    KUBECONFIG="${kubeconfig_path}" aws eks update-kubeconfig \
        --name "${cluster_name}" \
        --region "${region}" \
        --alias "${cluster_name}" > /dev/null

    KUBECONFIG="${kubeconfig_path}" kubectl get nodes -o json \
        | jq '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length'
}

wait_for_secondary_ready_nodes() {
    local cluster_name="$1"
    local region="$2"
    local timeout="${SECONDARY_CLUSTER_READY_TIMEOUT:-900}"
    local poll_interval="${SECONDARY_CLUSTER_POLL_INTERVAL:-10}"
    local kubeconfig_path
    local ready_nodes
    local elapsed=0

    kubeconfig_path="$(mktemp)"

    while [ "${elapsed}" -le "${timeout}" ]; do
        ready_nodes="$(ready_nodes_for_cluster "${cluster_name}" "${region}" "${kubeconfig_path}" 2>/dev/null || echo "0")"
        if [ "${ready_nodes}" -ge 1 ]; then
            echo "${ready_nodes}"
            rm -f "${kubeconfig_path}"
            return 0
        fi
        echo "Waiting for secondary cluster node readiness (${ready_nodes} Ready node(s))..." >&2
        sleep "${poll_interval}"
        elapsed=$((elapsed + poll_interval))
    done

    echo "Error: secondary cluster '${cluster_name}' did not report a Ready node within ${timeout}s" >&2
    rm -f "${kubeconfig_path}"
    return 1
}

PRIMARY_CLUSTER_NAME=$(terraform -chdir="${CLUSTER_TF_DIR}" output -raw cluster_name)
AWS_REGION=$(terraform -chdir="${CLUSTER_TF_DIR}" output -raw region)

echo "" >&2
echo "========================================" >&2
echo "  Creating secondary EKS shared-VPC cluster" >&2
echo "========================================" >&2
echo "  primary cluster: ${PRIMARY_CLUSTER_NAME}" >&2
echo "  region: ${AWS_REGION}" >&2
echo "  state file: ${STATE_FILE}" >&2
echo "" >&2

cd "${TF_DIR}"

echo "Initializing Terraform..." >&2
terraform init >&2

TF_AUTO_APPROVE="${TF_AUTO_APPROVE:-false}"
if [ "${TF_AUTO_APPROVE}" = "true" ]; then
    terraform apply -auto-approve -state="${STATE_FILE}" >&2
else
    terraform apply -state="${STATE_FILE}" >&2
fi

SECONDARY_CLUSTER_NAME=$(terraform output -state="${STATE_FILE}" -raw cluster_name)

cd - > /dev/null

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
PRIMARY_INFO=$(aws eks describe-cluster --name "${PRIMARY_CLUSTER_NAME}" --region "${AWS_REGION}" --output json)
SECONDARY_INFO=$(aws eks describe-cluster --name "${SECONDARY_CLUSTER_NAME}" --region "${AWS_REGION}" --output json)

PRIMARY_STATUS=$(echo "${PRIMARY_INFO}" | jq -r '.cluster.status // empty')
PRIMARY_VPC_ID=$(echo "${PRIMARY_INFO}" | jq -r '.cluster.resourcesVpcConfig.vpcId // empty')
SECONDARY_STATUS=$(echo "${SECONDARY_INFO}" | jq -r '.cluster.status // empty')
SECONDARY_VPC_ID=$(echo "${SECONDARY_INFO}" | jq -r '.cluster.resourcesVpcConfig.vpcId // empty')

SECONDARY_READY_NODES=$(wait_for_secondary_ready_nodes "${SECONDARY_CLUSTER_NAME}" "${AWS_REGION}")

jq -n \
    --arg tenancy_id "${ACCOUNT_ID}" \
    --arg network_id "${PRIMARY_VPC_ID}" \
    --arg primary_name "${PRIMARY_CLUSTER_NAME}" \
    --arg primary_status "${PRIMARY_STATUS}" \
    --arg primary_vpc_id "${PRIMARY_VPC_ID}" \
    --arg secondary_name "${SECONDARY_CLUSTER_NAME}" \
    --arg secondary_status "${SECONDARY_STATUS}" \
    --arg secondary_vpc_id "${SECONDARY_VPC_ID}" \
    --argjson secondary_ready_nodes "${SECONDARY_READY_NODES}" \
    '{
      success: true,
      platform: "kubernetes",
      test_id: "K8S26-01",
      tenancy_id: $tenancy_id,
      network_id: $network_id,
      clusters: [
        {
          name: $primary_name,
          role: "primary",
          tenancy_id: $tenancy_id,
          network_id: $primary_vpc_id,
          status: $primary_status
        },
        {
          name: $secondary_name,
          role: "secondary",
          tenancy_id: $tenancy_id,
          network_id: $secondary_vpc_id,
          status: $secondary_status,
          ready_node_count: $secondary_ready_nodes
        }
      ]
    }'
