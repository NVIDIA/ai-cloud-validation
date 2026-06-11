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

# Shared K8s Inventory Logic
#
# Sourced by provider-specific setup.sh scripts. Expects the caller to set:
#   KUBECTL           - kubectl command (e.g. "kubectl", "microk8s kubectl", "k3s kubectl")
#   CLUSTER_NAME      - cluster identifier
#   DEFAULT_GPU_NS    - fallback GPU operator namespace (default: nvidia-gpu-operator)
#   USE_NVIDIA_SMI_FALLBACK - "true" to fall back to nvidia-smi for GPU/driver info (default: false)
#   REQUIRE_JQ        - "true" to require jq and populate nodes array (default: false)
#
# Output: prints JSON inventory to stdout

DEFAULT_GPU_NS="${DEFAULT_GPU_NS:-nvidia-gpu-operator}"
USE_NVIDIA_SMI_FALLBACK="${USE_NVIDIA_SMI_FALLBACK:-false}"
REQUIRE_JQ="${REQUIRE_JQ:-false}"

# --- jq check (only when required) ---
if [ "$REQUIRE_JQ" = "true" ] && ! command -v jq &> /dev/null; then
    echo "Error: jq not found - required for JSON processing" >&2
    exit 1
fi

# --- Cluster connectivity ---
if ! $KUBECTL cluster-info &> /dev/null; then
    echo "Error: Cannot connect to Kubernetes cluster (using: $KUBECTL)" >&2
    exit 1
fi

# --- Node info ---
NODE_COUNT=$($KUBECTL get nodes --no-headers 2>/dev/null | wc -l)

if [ "$REQUIRE_JQ" = "true" ]; then
    NODES=$($KUBECTL get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | jq -R . | jq -s .)
else
    NODES="[]"
fi

# --- GPU info ---
GPU_NODE_COUNT=$($KUBECTL get nodes -l nvidia.com/gpu.present=true -o name 2>/dev/null | wc -l || echo "0")
GPU_PER_NODE=$($KUBECTL get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].status.capacity.nvidia\.com/gpu}' 2>/dev/null || echo "0")
if [ -z "$GPU_PER_NODE" ] || [ "$GPU_PER_NODE" = "null" ]; then
    if [ "$USE_NVIDIA_SMI_FALLBACK" = "true" ] && command -v nvidia-smi &> /dev/null; then
        GPU_PER_NODE=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l || echo "0")
    else
        GPU_PER_NODE=0
    fi
fi

TOTAL_GPUS=$((GPU_NODE_COUNT * GPU_PER_NODE))

# --- Driver version from node labels ---
DRIVER_MAJOR=$($KUBECTL get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.major}' 2>/dev/null || echo "")
DRIVER_MINOR=$($KUBECTL get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.minor}' 2>/dev/null || echo "")
DRIVER_REV=$($KUBECTL get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.rev}' 2>/dev/null || echo "")

if [ -n "$DRIVER_MAJOR" ] && [ -n "$DRIVER_MINOR" ] && [ -n "$DRIVER_REV" ]; then
    DRIVER_VERSION="${DRIVER_MAJOR}.${DRIVER_MINOR}.${DRIVER_REV}"
elif [ -n "$DRIVER_MAJOR" ] && [ -n "$DRIVER_MINOR" ]; then
    DRIVER_VERSION="${DRIVER_MAJOR}.${DRIVER_MINOR}"
elif [ -n "$DRIVER_MAJOR" ]; then
    DRIVER_VERSION="${DRIVER_MAJOR}"
elif [ "$USE_NVIDIA_SMI_FALLBACK" = "true" ] && command -v nvidia-smi &> /dev/null; then
    DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
else
    DRIVER_VERSION="unknown"
fi

# --- GPU operator namespace ---
GPU_OPERATOR_NS=""
for ns in gpu-operator gpu-operator-resources nvidia-gpu-operator; do
    if $KUBECTL get namespace "$ns" &> /dev/null; then
        GPU_OPERATOR_NS="$ns"
        break
    fi
done
GPU_OPERATOR_NS="${GPU_OPERATOR_NS:-$DEFAULT_GPU_NS}"

# --- Control-plane namespace (where apiserver/scheduler/controller-manager run) ---
CONTROL_PLANE_NS=""
for ns in kube-system openshift-kube-apiserver; do
    if $KUBECTL get pods -n "$ns" -l component=kube-apiserver --no-headers 2>/dev/null | grep -q .; then
        CONTROL_PLANE_NS="$ns"
        break
    fi
done
CONTROL_PLANE_NS="${CONTROL_PLANE_NS:-kube-system}"

# --- Runtime class ---
RUNTIME_CLASS=""
if $KUBECTL get runtimeclass nvidia &> /dev/null; then
    RUNTIME_CLASS="nvidia"
fi

# --- CSI StorageClasses ---
# Env overrides take precedence; otherwise auto-detect from installed
# CSIDrivers and the provisioners referenced by existing StorageClasses.
# Shared-fs / NFS classification is a best-effort name match against common
# provisioners; anything else is treated as block-capable.
CSI_BLOCK_SC="${K8S_CSI_BLOCK_SC:-}"
CSI_SHARED_FS_SC="${K8S_CSI_SHARED_FS_SC:-}"
CSI_NFS_SC="${K8S_CSI_NFS_SC:-}"

if [ -z "$CSI_BLOCK_SC" ] || [ -z "$CSI_SHARED_FS_SC" ] || [ -z "$CSI_NFS_SC" ]; then
    CSI_DRIVERS=$($KUBECTL get csidrivers -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")
    if [ -n "$CSI_DRIVERS" ]; then
        SC_LINES=$($KUBECTL get sc -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.provisioner}{"\n"}{end}' 2>/dev/null || echo "")
        BLOCK_LIKE_SC=""
        SHARED_LIKE_SC=""
        NFS_LIKE_SC=""
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            sc_name="${line%%=*}"
            sc_prov="${line#*=}"
            case " $CSI_DRIVERS " in
                *" $sc_prov "*) ;;
                *) continue ;;
            esac
            case "$sc_prov" in
                *nfs*) [ -z "$NFS_LIKE_SC" ] && NFS_LIKE_SC="$sc_name" ;;
            esac
            case "$sc_prov" in
                *nfs*|*efs*|*cephfs*|*filestore*|*azurefile*)
                    [ -z "$SHARED_LIKE_SC" ] && SHARED_LIKE_SC="$sc_name" ;;
                *)
                    [ -z "$BLOCK_LIKE_SC" ] && BLOCK_LIKE_SC="$sc_name" ;;
            esac
        done <<< "$SC_LINES"
        [ -z "$CSI_BLOCK_SC" ] && CSI_BLOCK_SC="$BLOCK_LIKE_SC"
        [ -z "$CSI_SHARED_FS_SC" ] && CSI_SHARED_FS_SC="$SHARED_LIKE_SC"
        [ -z "$CSI_NFS_SC" ] && CSI_NFS_SC="$NFS_LIKE_SC"
    fi
fi

# --- Output JSON ---
cat << EOF
{
  "success": true,
  "platform": "kubernetes",
  "cluster_name": "${CLUSTER_NAME}",
  "kubernetes": {
    "driver_version": "${DRIVER_VERSION}",
    "node_count": ${NODE_COUNT},
    "nodes": ${NODES},
    "gpu_node_count": ${GPU_NODE_COUNT},
    "gpu_per_node": ${GPU_PER_NODE},
    "total_gpus": ${TOTAL_GPUS},
    "gpu_operator_namespace": "${GPU_OPERATOR_NS}",
    "control_plane_namespace": "${CONTROL_PLANE_NS}",
    "runtime_class": "${RUNTIME_CLASS}",
    "gpu_resource_name": "nvidia.com/gpu"
  },
  "csi": {
    "block_storage_class": "${CSI_BLOCK_SC}",
    "shared_fs_storage_class": "${CSI_SHARED_FS_SC}",
    "nfs_storage_class": "${CSI_NFS_SC}",
    "static_volume_handle": "",
    "static_driver_name": ""
  }
}
EOF
