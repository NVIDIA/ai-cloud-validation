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

# k3s Inventory Stub - Queries local k3s cluster
#
# Requirements:
#   - k3s installed and running
#   - kubectl or k3s kubectl available
#   - KUBECONFIG set or /etc/rancher/k3s/k3s.yaml readable

set -eo pipefail

# Prefer k3s kubectl (reads its own kubeconfig automatically)
if [[ "${KUBECTL:-}" =~ [^[:space:]] ]]; then
    :  # already set from environment; skip detection
elif command -v k3s &> /dev/null; then
    KUBECTL="k3s kubectl"
elif command -v kubectl &> /dev/null; then
    KUBECTL="kubectl"
else
    echo "Error: Neither k3s nor kubectl found. Set KUBECTL to override." >&2
    exit 1
fi

# Set KUBECONFIG for k3s if not already set and default config exists
if [[ -z "$KUBECONFIG" && -f /etc/rancher/k3s/k3s.yaml ]]; then
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
fi

CLUSTER_NAME="k3s-$(hostname)"
DEFAULT_GPU_NS="gpu-operator"
USE_NVIDIA_SMI_FALLBACK="true"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"
