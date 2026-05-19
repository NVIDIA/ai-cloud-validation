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

# Apply the test node pool via Terraform on AWS EKS.
#
# Applies ./terraform-node-pool/ against the existing cluster state and emits
# a JSON payload matching the `node_pool` output schema. Because terraform
# apply is idempotent, this script is used for both the initial create and
# subsequent updates (e.g. scale via a new TF_VAR_test_pool_desired_size);
# NODE_POOL_ACTION controls only the log banner. The JSON is consumed by
# K8sNodePoolCheck via {{steps.create_test_node_pool.*}} /
# {{steps.update_test_node_pool.*}} in the suite config.
#
# Environment variables (all optional):
#   NODE_POOL_ACTION                  - Banner verb: "Creating" | "Updating"
#                                       (default "Creating")
#   TF_AUTO_APPROVE                   - "true" to skip approval (default: false)
#   TF_VAR_region                     - AWS region (default from cluster state)
#   TF_VAR_test_pool_name             - Node pool name (default "isv-test-pool")
#   TF_VAR_test_pool_instance_types   - JSON array of instance types
#                                       (default '["m6i.large"]')
#   TF_VAR_test_pool_desired_size     - Node count (default 1; bump on update
#                                       to scale)
#   TF_VAR_test_pool_ami_type         - EKS AMI type
#                                       (default "AL2023_x86_64_STANDARD")
#   TF_VAR_test_pool_capacity_type    - ON_DEMAND | SPOT (default ON_DEMAND)
#   TF_VAR_test_pool_labels_json      - JSON object of extra labels (default "{}")
#   TF_VAR_test_pool_taints_json      - JSON array of taints with Kubernetes
#                                       effect spelling (NoSchedule, etc.)
#   TF_VAR_test_pool_node_type        - Informational "cpu" | "gpu" (default "cpu")

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform-node-pool"
CLUSTER_TF_DIR="${SCRIPT_DIR}/terraform"

if ! command -v terraform &> /dev/null; then
    echo "Error: terraform not found - install from https://terraform.io" >&2
    exit 1
fi
if ! command -v jq &> /dev/null; then
    echo "Error: jq not found" >&2
    exit 1
fi
if [ ! -f "${CLUSTER_TF_DIR}/terraform.tfstate" ]; then
    echo "Error: cluster state not found at ${CLUSTER_TF_DIR}/terraform.tfstate" >&2
    echo "Run the cluster setup step first (provisions the EKS cluster)." >&2
    exit 1
fi

# Defaults for user-facing knobs. We accept JSON via env vars so callers can
# pass labels/taints without building Terraform var files.
NODE_POOL_NAME="${TF_VAR_test_pool_name:-isv-test-pool}"
INSTANCE_TYPES_JSON="${TF_VAR_test_pool_instance_types:-[\"m6i.large\"]}"
DESIRED_SIZE="${TF_VAR_test_pool_desired_size:-1}"
AMI_TYPE="${TF_VAR_test_pool_ami_type:-AL2023_x86_64_STANDARD}"
CAPACITY_TYPE="${TF_VAR_test_pool_capacity_type:-ON_DEMAND}"
LABELS_JSON="${TF_VAR_test_pool_labels_json:-"{}"}"
TAINTS_JSON="${TF_VAR_test_pool_taints_json:-[]}"
NODE_TYPE="${TF_VAR_test_pool_node_type:-cpu}"
ACTION="${NODE_POOL_ACTION:-Creating}"

# Validate JSON inputs up front with clear error messages - TF's own errors
# for malformed HCL vars are hard to read.
echo "${INSTANCE_TYPES_JSON}" | jq -e 'type == "array"' > /dev/null \
    || { echo "Error: TF_VAR_test_pool_instance_types must be a JSON array" >&2; exit 1; }
echo "${LABELS_JSON}" | jq -e 'type == "object"' > /dev/null \
    || { echo "Error: TF_VAR_test_pool_labels_json must be a JSON object" >&2; exit 1; }
echo "${TAINTS_JSON}" | jq -e 'type == "array"' > /dev/null \
    || { echo "Error: TF_VAR_test_pool_taints_json must be a JSON array" >&2; exit 1; }

NODE_TYPE="$(echo "${NODE_TYPE}" | tr '[:upper:]' '[:lower:]')"
case "${NODE_TYPE}" in
    cpu|gpu) ;;
    *)
        echo "Error: TF_VAR_test_pool_node_type must be one of: cpu, gpu" >&2
        exit 1
        ;;
esac

echo "" >&2
echo "========================================" >&2
echo "  ${ACTION} test node pool" >&2
echo "========================================" >&2
echo "  pool name: ${NODE_POOL_NAME}" >&2
echo "  instance types: ${INSTANCE_TYPES_JSON}" >&2
echo "  desired size: ${DESIRED_SIZE}" >&2
echo "  ami type: ${AMI_TYPE}" >&2
echo "  capacity: ${CAPACITY_TYPE}" >&2
echo "  node type tag: ${NODE_TYPE}" >&2
echo "" >&2

cd "${TF_DIR}"

echo "Initializing Terraform..." >&2
terraform init >&2

# Map the user-facing JSON env vars onto the module's variable names. Using
# HCL-compatible JSON means we can forward directly with TF_VAR_*.
export TF_VAR_node_pool_name="${NODE_POOL_NAME}"
export TF_VAR_instance_types="${INSTANCE_TYPES_JSON}"
export TF_VAR_desired_size="${DESIRED_SIZE}"
export TF_VAR_ami_type="${AMI_TYPE}"
export TF_VAR_capacity_type="${CAPACITY_TYPE}"
export TF_VAR_labels="${LABELS_JSON}"
export TF_VAR_taints="${TAINTS_JSON}"

TF_AUTO_APPROVE="${TF_AUTO_APPROVE:-false}"
if [ "${TF_AUTO_APPROVE}" = "true" ]; then
    terraform apply -auto-approve >&2
else
    terraform apply >&2
fi

# Read what Terraform actually created. expected_taints comes back with
# Kubernetes effect spelling - the validation compares directly against
# kubectl, so no further translation is needed.
TF_OUT_NODE_POOL_NAME=$(terraform output -raw node_pool_name)
TF_OUT_LABEL_SELECTOR=$(terraform output -raw label_selector)
TF_OUT_DESIRED_SIZE=$(terraform output -raw desired_size)
TF_OUT_EXPECTED_LABELS=$(terraform output -json expected_labels)
TF_OUT_EXPECTED_TAINTS=$(terraform output -json expected_taints)
TF_OUT_EXPECTED_INSTANCE_TYPES=$(terraform output -json expected_instance_types)

cd - > /dev/null

# Jinja templating in the suite config renders step outputs as strings; to
# preserve structure, we store labels/taints/instance-types as *JSON
# strings* inside the payload. The validation accepts either native
# collections or JSON strings.
EXPECTED_LABELS_COMPACT=$(echo "${TF_OUT_EXPECTED_LABELS}" | jq -c .)
EXPECTED_TAINTS_COMPACT=$(echo "${TF_OUT_EXPECTED_TAINTS}" | jq -c .)
EXPECTED_INSTANCE_TYPES_COMPACT=$(echo "${TF_OUT_EXPECTED_INSTANCE_TYPES}" | jq -c .)

jq -n \
    --arg node_pool_name "${TF_OUT_NODE_POOL_NAME}" \
    --arg label_selector "${TF_OUT_LABEL_SELECTOR}" \
    --argjson expected_replicas "${TF_OUT_DESIRED_SIZE}" \
    --arg expected_labels_json "${EXPECTED_LABELS_COMPACT}" \
    --arg expected_taints_json "${EXPECTED_TAINTS_COMPACT}" \
    --arg expected_instance_types_json "${EXPECTED_INSTANCE_TYPES_COMPACT}" \
    --arg node_type "${NODE_TYPE}" \
    '{
      success: true,
      platform: "kubernetes",
      node_pool_name: $node_pool_name,
      label_selector: $label_selector,
      expected_replicas: $expected_replicas,
      expected_labels_json: $expected_labels_json,
      expected_taints_json: $expected_taints_json,
      expected_instance_types_json: $expected_instance_types_json,
      node_type: $node_type
    }'
