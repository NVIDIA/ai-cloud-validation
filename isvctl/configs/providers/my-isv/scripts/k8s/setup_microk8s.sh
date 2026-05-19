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

# MicroK8s Inventory Stub - Queries local MicroK8s cluster
#
# Requirements:
#   - MicroK8s installed and running
#   - microk8s kubectl or kubectl configured

set -eo pipefail

# Detect kubectl command (microk8s or regular)
if [[ "${KUBECTL:-}" =~ [^[:space:]] ]]; then
    :  # already set from environment; skip detection
elif command -v microk8s &> /dev/null; then
    KUBECTL="microk8s kubectl"
elif command -v kubectl &> /dev/null; then
    KUBECTL="kubectl"
else
    echo "Error: Neither microk8s nor kubectl found. Set KUBECTL to override." >&2
    exit 1
fi

CLUSTER_NAME="microk8s-$(hostname)"
DEFAULT_GPU_NS="gpu-operator-resources"
USE_NVIDIA_SMI_FALLBACK="true"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"
