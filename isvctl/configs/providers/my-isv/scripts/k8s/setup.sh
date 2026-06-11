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

# K8s Inventory Stub - Queries real cluster and outputs inventory JSON
#
# Requirements:
#   - kubectl OR microk8s configured and accessible
#   - jq for JSON processing
#   - nvidia GPU operator installed (for GPU detection)

set -eo pipefail

# Detect kubectl command
if [[ "${KUBECTL:-}" =~ [^[:space:]] ]]; then
    :  # already set from environment; skip detection
elif command -v kubectl &> /dev/null; then
    KUBECTL="kubectl"
elif command -v microk8s &> /dev/null; then
    KUBECTL="microk8s kubectl"
else
    echo "Error: Neither kubectl nor microk8s found. Set KUBECTL to override." >&2
    exit 1
fi

CLUSTER_NAME=$($KUBECTL config current-context 2>/dev/null || echo "unknown")
DEFAULT_GPU_NS="unknown"
REQUIRE_JQ="true"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"
