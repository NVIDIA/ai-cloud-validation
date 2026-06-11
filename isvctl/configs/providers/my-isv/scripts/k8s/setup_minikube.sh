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

# Minikube Inventory Stub - Queries local Minikube cluster
#
# Requirements:
#   - Minikube installed and running
#   - kubectl configured (minikube automatically configures kubeconfig)

set -eo pipefail

# Detect kubectl command
if [[ "${KUBECTL:-}" =~ [^[:space:]] ]]; then
    :  # already set from environment; skip detection
elif command -v kubectl &> /dev/null; then
    KUBECTL="kubectl"
else
    echo "Error: kubectl not found. Set KUBECTL to override." >&2
    exit 1
fi

# Get cluster name from minikube profile or kubectl context
if command -v minikube &> /dev/null; then
    CLUSTER_NAME=$(minikube profile 2>/dev/null || echo "minikube")
else
    CLUSTER_NAME=$($KUBECTL config current-context 2>/dev/null || echo "minikube")
fi

DEFAULT_GPU_NS="${DEFAULT_GPU_NS:-gpu-operator}"
USE_NVIDIA_SMI_FALLBACK="${USE_NVIDIA_SMI_FALLBACK:-true}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"
