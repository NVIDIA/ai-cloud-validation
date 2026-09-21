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

# Kubernetes versions MicroK8s can install (K8S02-01). MicroK8s ships as a snap
# with one track per minor, so the snap's track list is the catalogue. It comes
# from the store rather than `snap info` so the answer is the same on a host
# without snapd - the catalogue is a property of the offering, not the machine.
# Matching plain X.Y also drops variant tracks (1.27-strict, 1.24-eksd) that
# repackage a minor already in the list.
MICROK8S_STORE="${MICROK8S_SNAP_INFO_URL:-https://api.snapcraft.io/v2/snaps/info/microk8s}"
OFFERED_VERSIONS=$(curl -sS --connect-timeout 3 --max-time 8 -H 'Snap-Device-Series: 16' "$MICROK8S_STORE" 2>/dev/null \
    | grep -o '"track":"[0-9]*\.[0-9]*"' \
    | sed 's/.*:"//;s/"//' \
    | sort -Vru || true)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"
