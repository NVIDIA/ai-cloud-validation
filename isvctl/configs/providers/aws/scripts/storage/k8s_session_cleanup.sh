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

# Storage kubernetes-session teardown: sweep namespaces leaked by the checks.
#
# The storage session checks create and delete their own namespaces
# (isvtest-csi-*, isvtest-fs-*, isvtest-kmod*); a check that crashed mid-run
# can leak one. This best-effort sweep keeps the session cluster clean for
# whatever runs after the storage bracket — the platform teardown that
# follows destroys the cluster anyway on ephemeral labs.
#
# Usage: k8s_session_cleanup.sh [--kubeconfig <path>]

set -o pipefail

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

LEAKED=$($KUBECTL get namespaces --no-headers -o custom-columns=":metadata.name" 2>/dev/null |
    grep -E '^isvtest-(csi|fs|kmod)' || true)

DELETED=0
CLEANUP_ERRORS=()
for ns in $LEAKED; do
    echo "Deleting leaked namespace: $ns" >&2
    if $KUBECTL delete namespace "$ns" --wait=false > /dev/null 2>&1; then
        DELETED=$((DELETED + 1))
    else
        CLEANUP_ERRORS+=("$ns")
    fi
done

if [[ ${#CLEANUP_ERRORS[@]} -gt 0 ]]; then
    ERRORS_JSON=$(printf '"%s",' "${CLEANUP_ERRORS[@]}")
    cat <<EOF
{"success": true, "platform": "storage", "deleted_namespaces": ${DELETED}, "cleanup_errors": [${ERRORS_JSON%,}]}
EOF
else
    cat <<EOF
{"success": true, "platform": "storage", "deleted_namespaces": ${DELETED}}
EOF
fi
