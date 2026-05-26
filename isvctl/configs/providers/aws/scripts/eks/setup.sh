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

# AWS EKS Setup Stub - Provisions AWS EKS cluster using Terraform
#
# This stub provisions an EKS GPU cluster using Terraform and outputs
# the cluster inventory JSON for ISV Lab validation testing.
#
# Requirements:
#   - terraform >= 1.5.0
#   - AWS CLI configured with appropriate credentials
#   - kubectl
#   - jq
#   - curl (unless TF_VAR_cluster_endpoint_public_access_cidrs is set)
#
# Environment Variables:
#   - TF_VAR_*: Terraform variables (e.g., TF_VAR_region, TF_VAR_gpu_node_instance_types)
#   - TF_VAR_cluster_endpoint_public_access_cidrs: Optional EKS API allowlist.
#     Defaults to the caller's auto-detected public IPv4 /32.
#   - TF_AUTO_APPROVE: Set to "true" to skip Terraform approval prompt (default: false)
#   - SKIP_PREFLIGHT: Set to "true" to skip infrastructure validation (default: false)
#
# Output: JSON inventory conforming to isvctl schema

set -eo pipefail

is_ipv4_address() {
    local ip="$1"
    local IFS=.
    local -a octets

    [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    read -r -a octets <<< "$ip"
    [ "${#octets[@]}" -eq 4 ] || return 1

    for octet in "${octets[@]}"; do
        if ((10#$octet > 255)); then
            return 1
        fi
    done

    return 0
}

detect_public_ipv4() {
    local url
    local ip

    for url in \
        "https://checkip.amazonaws.com" \
        "https://api.ipify.org" \
        "https://ifconfig.me/ip"; do
        ip="$(curl -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
        if is_ipv4_address "$ip"; then
            echo "$ip"
            return 0
        fi
    done

    return 1
}

configure_eks_endpoint_allowlist() {
    if [ -n "${TF_VAR_cluster_endpoint_public_access_cidrs:-}" ]; then
        echo "Configured EKS API allowlist for Terraform: ${TF_VAR_cluster_endpoint_public_access_cidrs}" >&2
        return
    fi

    if ! command -v curl &> /dev/null; then
        echo "Error: curl not found - required to auto-detect the EKS API allowlist." >&2
        echo "Set TF_VAR_cluster_endpoint_public_access_cidrs='[\"YOUR.IP.ADDRESS/32\"]' to bypass detection." >&2
        exit 1
    fi

    echo "Detecting caller public IPv4 for EKS API allowlist..." >&2

    local public_ip
    public_ip="$(detect_public_ipv4 || true)"
    if [ -z "$public_ip" ]; then
        echo "Error: Could not auto-detect caller public IPv4 for the EKS API allowlist." >&2
        echo "Set TF_VAR_cluster_endpoint_public_access_cidrs='[\"YOUR.IP.ADDRESS/32\"]' and retry." >&2
        exit 1
    fi

    export TF_VAR_cluster_endpoint_public_access_cidrs="[\"${public_ip}/32\"]"
    echo "Configured EKS API allowlist for Terraform: ${TF_VAR_cluster_endpoint_public_access_cidrs}" >&2
}

# Get script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="${SCRIPT_DIR}/terraform"

# -----------------------------------------------------------------------------
# Dependency Checks
# -----------------------------------------------------------------------------

echo "Checking dependencies..." >&2

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

if [ ! -d "$TERRAFORM_DIR" ]; then
    echo "Error: Terraform directory not found: $TERRAFORM_DIR" >&2
    exit 1
fi

if ! aws sts get-caller-identity &> /dev/null 2>&1; then
    echo "Error: AWS credentials not configured" >&2
    exit 1
fi

configure_eks_endpoint_allowlist

# -----------------------------------------------------------------------------
# Terraform Provisioning
# -----------------------------------------------------------------------------

echo "" >&2
echo "========================================" >&2
echo "Provisioning AWS EKS GPU Cluster" >&2
echo "========================================" >&2
echo "" >&2

cd "$TERRAFORM_DIR"

# Initialize Terraform
echo "Initializing Terraform..." >&2
terraform init -upgrade >&2

# Get expected cluster name from Terraform vars
TF_CLUSTER_PREFIX="${TF_VAR_cluster_name_prefix:-isv-gpu}"
TF_ENVIRONMENT="${TF_VAR_environment:-dev}"
EXPECTED_CLUSTER="${TF_CLUSTER_PREFIX}-${TF_ENVIRONMENT}"
TF_REGION="${TF_VAR_region:-$(aws configure get region 2>/dev/null || echo "us-west-2")}"

# Check if cluster already exists in AWS
EXISTING_CLUSTER=$(aws eks describe-cluster --name "$EXPECTED_CLUSTER" --region "$TF_REGION" 2>/dev/null && echo "exists" || echo "")

# Check if state already has resources
STATE_RESOURCES=$(terraform state list 2>/dev/null | wc -l || echo "0")

if [ "$STATE_RESOURCES" -gt 0 ]; then
    echo "Terraform state exists with $STATE_RESOURCES resources" >&2
    echo "Running terraform refresh to sync state..." >&2
    terraform refresh >&2
elif [ -n "$EXISTING_CLUSTER" ]; then
    # Cluster exists but not in Terraform state - just use it
    echo "" >&2
    echo "Cluster '$EXPECTED_CLUSTER' already exists in AWS" >&2
    echo "Skipping Terraform provisioning - using existing cluster" >&2
    echo "" >&2
    AWS_REGION="$TF_REGION"
    EKS_CLUSTER_NAME="$EXPECTED_CLUSTER"
else
    # No state and no existing cluster - create new
    echo "" >&2
    echo "Provisioning new cluster..." >&2

    TF_AUTO_APPROVE="${TF_AUTO_APPROVE:-false}"
    if [ "$TF_AUTO_APPROVE" = "true" ]; then
        echo "Applying Terraform (auto-approved)..." >&2
        terraform apply -auto-approve >&2
    else
        echo "Applying Terraform..." >&2
        terraform apply >&2
    fi
fi

# Get outputs from Terraform (if available)
TF_REGION_OUTPUT=$(terraform output -raw region 2>/dev/null || echo "")
TF_CLUSTER_OUTPUT=$(terraform output -raw cluster_name 2>/dev/null || echo "")

cd - > /dev/null

# Use Terraform outputs if available, otherwise use detected values
if [ -n "$TF_REGION_OUTPUT" ]; then
    AWS_REGION="$TF_REGION_OUTPUT"
elif [ -z "$AWS_REGION" ]; then
    AWS_REGION="$TF_REGION"
fi

if [ -n "$TF_CLUSTER_OUTPUT" ]; then
    EKS_CLUSTER_NAME="$TF_CLUSTER_OUTPUT"
elif [ -z "$EKS_CLUSTER_NAME" ]; then
    EKS_CLUSTER_NAME="$EXPECTED_CLUSTER"
fi

if [ -z "$EKS_CLUSTER_NAME" ]; then
    echo "Error: Could not determine EKS cluster name" >&2
    exit 1
fi

echo "" >&2
echo "Cluster ready!" >&2
echo "  Region: $AWS_REGION" >&2
echo "  Cluster: $EKS_CLUSTER_NAME" >&2

# -----------------------------------------------------------------------------
# Configure kubectl
# -----------------------------------------------------------------------------

echo "" >&2
echo "Configuring kubectl..." >&2
aws eks update-kubeconfig --name "$EKS_CLUSTER_NAME" --region "$AWS_REGION" >&2

if ! kubectl cluster-info &> /dev/null 2>&1; then
    echo "Error: Cannot connect to EKS cluster" >&2
    echo "Hint: re-running setup against a pre-existing cluster does not push the configured" >&2
    echo "      EKS API allowlist (TF_VAR_cluster_endpoint_public_access_cidrs). If you are" >&2
    echo "      running from a different IP than the original apply, run 'terraform apply' in" >&2
    echo "      $TERRAFORM_DIR or run the teardown phase and re-create the cluster." >&2
    exit 1
fi

# -----------------------------------------------------------------------------
# Preflight Checks
# -----------------------------------------------------------------------------

SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-false}"

if [ "$SKIP_PREFLIGHT" != "true" ]; then
    echo "" >&2
    echo "Running preflight checks..." >&2

    # Wait for GPU nodes to be labeled by GPU Operator
    echo "  Waiting for GPU nodes..." >&2
    GPU_NODES=0
    for i in {1..30}; do
        GPU_NODES=$(kubectl get nodes -l nvidia.com/gpu.present=true -o name 2>/dev/null | wc -l || echo "0")
        if [ "$GPU_NODES" -gt 0 ]; then
            echo "    Found $GPU_NODES GPU node(s)" >&2
            break
        fi
        echo "    Waiting for GPU operator to label nodes... ($i/30)" >&2
        sleep 10
    done

    if [ "$GPU_NODES" -eq 0 ]; then
        echo "" >&2
        echo "Error: No GPU nodes detected after waiting 5 minutes." >&2
        echo "Ensure GPU node group is running and GPU Operator is installed." >&2
        echo "Check node status: kubectl get nodes -l nvidia.com/gpu.present=true" >&2
        echo "Check GPU Operator: kubectl get pods -n gpu-operator" >&2
        exit 1
    fi

    # Wait for GPU capacity and driver labels to be populated
    # These are set by the GPU Operator after driver installation completes
    echo "  Waiting for GPU driver and capacity labels..." >&2
    GPU_READY=false
    for i in {1..30}; do
        GPU_CAP=$(kubectl get nodes -l nvidia.com/gpu.present=true \
            -o jsonpath='{.items[0].status.capacity.nvidia\.com/gpu}' 2>/dev/null || echo "")
        DRIVER_LABEL=$(kubectl get nodes -l nvidia.com/gpu.present=true \
            -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.major}' 2>/dev/null || echo "")
        if [ -n "$GPU_CAP" ] && [ "$GPU_CAP" != "0" ] && [ -n "$DRIVER_LABEL" ]; then
            echo "    GPU capacity: $GPU_CAP, driver label: $DRIVER_LABEL" >&2
            GPU_READY=true
            break
        fi
        echo "    Waiting for GPU operator to finish driver setup... ($i/30)" >&2
        sleep 10
    done

    if [ "$GPU_READY" != "true" ]; then
        echo "" >&2
        echo "Error: GPU capacity/driver labels not ready after waiting 5 minutes." >&2
        echo "Check GPU Operator status: kubectl get pods -n gpu-operator" >&2
        echo "Check node labels: kubectl get nodes -l nvidia.com/gpu.present=true -o yaml" >&2
        exit 1
    fi

    # Check GPU Operator
    GPU_OP_NS=""
    for ns in gpu-operator nvidia-gpu-operator; do
        if kubectl get namespace "$ns" &> /dev/null 2>&1; then
            GPU_OP_NS="$ns"
            break
        fi
    done
    if [ -n "$GPU_OP_NS" ]; then
        echo "  GPU Operator namespace: $GPU_OP_NS" >&2
    else
        echo "  Warning: GPU Operator namespace not found" >&2
    fi

    # Check NGC credentials
    if [ -z "${NGC_API_KEY:-}" ]; then
        echo "  Warning: NGC_API_KEY not set (required for NIM workloads)" >&2
    else
        echo "  NGC_API_KEY: set" >&2
    fi

    echo "" >&2
fi

# -----------------------------------------------------------------------------
# Gather Cluster Information
# -----------------------------------------------------------------------------

EKS_INFO=$(aws eks describe-cluster --name "$EKS_CLUSTER_NAME" --region "$AWS_REGION" --output json)
CLUSTER_ENDPOINT=$(echo "$EKS_INFO" | jq -r '.cluster.endpoint // empty')
K8S_VERSION=$(echo "$EKS_INFO" | jq -r '.cluster.version // empty')
VPC_ID=$(echo "$EKS_INFO" | jq -r '.cluster.resourcesVpcConfig.vpcId // empty')

NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)

NODES=$(kubectl get nodes -o json 2>/dev/null | jq '[.items[] | {
    name: .metadata.name,
    ip: (if .status.addresses then (.status.addresses | map(select(.type == "InternalIP")) | .[0].address) else null end),
    gpus: (if .status.capacity["nvidia.com/gpu"] then (.status.capacity["nvidia.com/gpu"] | tonumber) else 0 end)
}]')

GPU_NODE_COUNT=$(kubectl get nodes -l nvidia.com/gpu.present=true -o name 2>/dev/null | wc -l || echo "0")

GPU_PER_NODE=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].status.capacity.nvidia\.com/gpu}' 2>/dev/null || echo "0")
[ -z "$GPU_PER_NODE" ] || [ "$GPU_PER_NODE" = "null" ] && GPU_PER_NODE=0

TOTAL_GPUS=$((GPU_NODE_COUNT * GPU_PER_NODE))

# GPU Operator namespace
GPU_OPERATOR_NS=""
for ns in gpu-operator nvidia-gpu-operator gpu-operator-resources; do
    if kubectl get namespace "$ns" &> /dev/null 2>&1; then
        GPU_OPERATOR_NS="$ns"
        break
    fi
done
GPU_OPERATOR_NS="${GPU_OPERATOR_NS:-gpu-operator}"

# Driver version
DRIVER_MAJOR=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.major}' 2>/dev/null || echo "")
DRIVER_MINOR=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.minor}' 2>/dev/null || echo "")
DRIVER_REV=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.rev}' 2>/dev/null || echo "")

if [ -n "$DRIVER_MAJOR" ] && [ -n "$DRIVER_MINOR" ] && [ -n "$DRIVER_REV" ]; then
    DRIVER_VERSION="${DRIVER_MAJOR}.${DRIVER_MINOR}.${DRIVER_REV}"
elif [ -n "$DRIVER_MAJOR" ] && [ -n "$DRIVER_MINOR" ]; then
    DRIVER_VERSION="${DRIVER_MAJOR}.${DRIVER_MINOR}"
else
    DRIVER_VERSION="unknown"
fi

# Runtime class
RUNTIME_CLASS=""
kubectl get runtimeclass nvidia &> /dev/null 2>&1 && RUNTIME_CLASS="nvidia"

# AWS-specific info
GPU_INSTANCE_TYPES=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[*].metadata.labels.node\.kubernetes\.io/instance-type}' 2>/dev/null | tr ' ' ',' || echo "")
GPU_PRODUCT=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.labels.nvidia\.com/gpu\.product}' 2>/dev/null || echo "")

KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/config}"

# -----------------------------------------------------------------------------
# CSI StorageClasses
# -----------------------------------------------------------------------------
# Detect via kubectl so this works for both freshly-provisioned and
# pre-existing clusters (the Terraform apply is skipped when the cluster
# already exists, so we can't rely on Terraform outputs here).
#
# EFS satisfies both shared-filesystem (RWX) and NFS semantics - the AWS EFS
# CSI driver mounts via NFSv4.1 - so when efs.csi.aws.com is installed we
# surface the same StorageClass under both keys.

BLOCK_SC=$(kubectl get sc -o json 2>/dev/null \
    | jq -r '[.items[] | select(.provisioner == "ebs.csi.aws.com") | .metadata.name] | .[0] // ""' \
    || echo "")
EFS_SC=$(kubectl get sc -o json 2>/dev/null \
    | jq -r '[.items[] | select(.provisioner == "efs.csi.aws.com") | .metadata.name] | .[0] // ""' \
    || echo "")

# -----------------------------------------------------------------------------
# Standalone EBS volume for static CSI provisioning
# -----------------------------------------------------------------------------
# K8sCsiProvisioningModesCheck needs a pre-provisioned backing volume that
# the CSI driver does not own, so it can verify static provisioning via a
# manually-created PV. We create a 1 GiB gp3 volume in a worker-node AZ and
# tag it so teardown.sh can find and delete it before terraform destroy.
# The volume is reused across runs: describe-volumes filters by our cluster
# tag first and only creates a new one when none is found.

STATIC_VOLUME_HANDLE=""
STATIC_DRIVER_NAME=""
if [ -n "$BLOCK_SC" ]; then
    NODE_AZ=$(kubectl get nodes -l nvidia.com/gpu.present=true \
        -o jsonpath='{.items[0].metadata.labels.topology\.kubernetes\.io/zone}' 2>/dev/null || echo "")
    if [ -z "$NODE_AZ" ]; then
        NODE_AZ=$(kubectl get nodes \
            -o jsonpath='{.items[0].metadata.labels.topology\.kubernetes\.io/zone}' 2>/dev/null || echo "")
    fi

    if [ -n "$NODE_AZ" ]; then
        STATIC_VOL_TAG="isv-ncp-static-csi-${EKS_CLUSTER_NAME}"
        EXISTING_VOL=$(aws ec2 describe-volumes \
            --filters "Name=tag:Name,Values=${STATIC_VOL_TAG}" "Name=status,Values=available,in-use,creating" \
            --region "$AWS_REGION" --output json 2>/dev/null \
            | jq -r '.Volumes[0].VolumeId // ""' 2>/dev/null || echo "")

        if [ -n "$EXISTING_VOL" ] && [ "$EXISTING_VOL" != "null" ]; then
            STATIC_VOLUME_HANDLE="$EXISTING_VOL"
            echo "Reusing standalone EBS volume for static CSI validation: $STATIC_VOLUME_HANDLE" >&2
        else
            echo "Creating standalone EBS volume for static CSI validation in $NODE_AZ..." >&2
            STATIC_VOLUME_HANDLE=$(aws ec2 create-volume \
                --availability-zone "$NODE_AZ" \
                --volume-type gp3 \
                --size 1 \
                --tag-specifications "ResourceType=volume,Tags=[{Key=Name,Value=${STATIC_VOL_TAG}},{Key=isv-ncp-validation-suite,Value=static-csi},{Key=cluster,Value=${EKS_CLUSTER_NAME}}]" \
                --region "$AWS_REGION" \
                --query 'VolumeId' --output text 2>/dev/null || echo "")
            if [ -n "$STATIC_VOLUME_HANDLE" ] && [ "$STATIC_VOLUME_HANDLE" != "None" ]; then
                echo "Created standalone EBS volume: $STATIC_VOLUME_HANDLE" >&2
                aws ec2 wait volume-available --volume-ids "$STATIC_VOLUME_HANDLE" --region "$AWS_REGION" >&2 || true
            else
                STATIC_VOLUME_HANDLE=""
                echo "Warning: failed to create standalone EBS volume; static CSI probe will skip" >&2
            fi
        fi

        if [ -n "$STATIC_VOLUME_HANDLE" ]; then
            STATIC_DRIVER_NAME="ebs.csi.aws.com"
        fi
    else
        echo "Warning: could not determine worker-node AZ; skipping standalone EBS volume creation" >&2
    fi
fi

# -----------------------------------------------------------------------------
# Output JSON Inventory
# -----------------------------------------------------------------------------

cat << EOF
{
  "success": true,
  "platform": "kubernetes",
  "cluster_name": "${EKS_CLUSTER_NAME}",
  "node_count": ${NODE_COUNT},
  "endpoint": "${CLUSTER_ENDPOINT}",
  "gpu_count": ${TOTAL_GPUS},
  "gpu_per_node": ${GPU_PER_NODE},
  "driver_version": "${DRIVER_VERSION}",
  "kubeconfig_path": "${KUBECONFIG_PATH}",
  "kubernetes": {
    "driver_version": "${DRIVER_VERSION}",
    "node_count": ${NODE_COUNT},
    "nodes": ${NODES},
    "gpu_node_count": ${GPU_NODE_COUNT},
    "gpu_per_node": ${GPU_PER_NODE},
    "total_gpus": ${TOTAL_GPUS},
    "control_plane_address": "${CLUSTER_ENDPOINT}",
    "kubeconfig_path": "${KUBECONFIG_PATH}",
    "gpu_operator_namespace": "${GPU_OPERATOR_NS}",
    "cluster_autoscaler_namespace": "kube-system",
    "cluster_autoscaler_deployment": "cluster-autoscaler",
    "runtime_class": "${RUNTIME_CLASS}",
    "gpu_resource_name": "nvidia.com/gpu"
  },
  "csi": {
    "block_storage_class": "${BLOCK_SC}",
    "shared_fs_storage_class": "${EFS_SC}",
    "nfs_storage_class": "${EFS_SC}",
    "static_volume_handle": "${STATIC_VOLUME_HANDLE}",
    "static_driver_name": "${STATIC_DRIVER_NAME}"
  },
  "aws": {
    "region": "${AWS_REGION}",
    "vpc_id": "${VPC_ID}",
    "eks_cluster_name": "${EKS_CLUSTER_NAME}",
    "kubernetes_version": "${K8S_VERSION}",
    "gpu_instance_types": "${GPU_INSTANCE_TYPES}",
    "gpu_product": "${GPU_PRODUCT}"
  }
}
EOF
