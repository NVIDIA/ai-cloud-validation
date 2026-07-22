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

# Storage kubernetes-session setup: verify the session cluster is reachable.
#
# Runs as the setup step of commands[storage@kubernetes], INSIDE the
# kubernetes platform session: the cluster (and its CSI drivers) were
# provisioned by the platform run's setup, whose outputs arrive here via
# {{ session.* }} args. The storage checks provision their own namespaces,
# so this step only has to prove the cluster is usable before they run.
#
# Usage: k8s_session_prepare.sh [--kubeconfig <path>]

set -eo pipefail

KUBECONFIG_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kubeconfig)
            KUBECONFIG_PATH="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -n "$KUBECONFIG_PATH" ]]; then
    export KUBECONFIG="$KUBECONFIG_PATH"
fi

if [[ "${KUBECTL:-}" =~ [^[:space:]] ]]; then
    :  # already set from environment; skip detection
elif command -v kubectl &> /dev/null; then
    KUBECTL="kubectl"
else
    echo "Error: kubectl not found. Set KUBECTL to override." >&2
    exit 1
fi

echo "Verifying session cluster access..." >&2
if ! $KUBECTL get nodes --no-headers > /dev/null 2>&1; then
    echo "Error: session cluster is not reachable via $KUBECTL." >&2
    cat <<EOF
{"success": false, "platform": "storage", "error": "session cluster not reachable", "error_type": "ClusterUnreachable"}
EOF
    exit 1
fi

STORAGE_CLASS_COUNT=$($KUBECTL get storageclass --no-headers 2>/dev/null | wc -l | tr -d ' ')
echo "Session cluster reachable (${STORAGE_CLASS_COUNT} storage classes visible)." >&2

cat <<EOF
{"success": true, "platform": "storage", "storage_class_count": ${STORAGE_CLASS_COUNT}}
EOF
