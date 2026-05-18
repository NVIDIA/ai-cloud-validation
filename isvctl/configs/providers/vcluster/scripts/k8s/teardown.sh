#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 vCluster Labs
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# vCluster K8s Teardown - Deletes the vCluster tenant cluster and cleans up
# the kubeconfig written by setup.sh.
#
# Environment variables:
#   VCLUSTER_NAME             - name of the tenant cluster (default: vcluster-isv-validation)
#   VCLUSTER_NAMESPACE        - namespace on the Control Plane Cluster (default: vcluster-isv-validation)
#   VCLUSTER_KUBECONFIG_PATH  - persisted kubeconfig path to remove (default: /tmp/vcluster-isv-validation.kubeconfig)

set -eo pipefail

VCLUSTER_NAME="${VCLUSTER_NAME:-vcluster-isv-validation}"
VCLUSTER_NAMESPACE="${VCLUSTER_NAMESPACE:-vcluster-isv-validation}"
VCLUSTER_KUBECONFIG_PATH="${VCLUSTER_KUBECONFIG_PATH:-/tmp/vcluster-isv-validation.kubeconfig}"
GPU_TAINT_STATE_FILE="${VCLUSTER_KUBECONFIG_PATH%.kubeconfig}-gpu-taints.txt"

# ---------------------------------------------------------------------------
# Restore nvidia.com/gpu:NoSchedule taints that setup.sh removed for CNCF
# conformance.  Uses the ambient KUBECONFIG (Control Plane Cluster).
# ---------------------------------------------------------------------------
if [ -f "$GPU_TAINT_STATE_FILE" ]; then
    echo "Restoring nvidia.com/gpu:NoSchedule taints on host nodes..." >&2
    while IFS= read -r node; do
        [ -z "$node" ] && continue
        if kubectl taint node "$node" nvidia.com/gpu:NoSchedule --overwrite >/dev/null 2>&1; then
            echo "  Restored taint on ${node}." >&2
        else
            echo "  Warning: could not restore taint on ${node} (node may have been deleted)." >&2
        fi
    done < "$GPU_TAINT_STATE_FILE"
    rm -f "$GPU_TAINT_STATE_FILE"
    echo "Taint restoration complete." >&2
fi

# ---------------------------------------------------------------------------
# Best-effort cleanup of legacy port-forward state (the current provider no
# longer starts one, but earlier iterations did; leave this in so an upgrade
# from an older setup.sh still removes the pidfile).
# ---------------------------------------------------------------------------
_PF_PIDFILE="/tmp/vcluster-portfwd-pid.txt"
if [ -f "$_PF_PIDFILE" ]; then
    _PF_PID=$(cat "$_PF_PIDFILE" 2>/dev/null || echo "")
    if [ -n "$_PF_PID" ]; then
        pkill -P "$_PF_PID" 2>/dev/null || true
        kill "$_PF_PID" 2>/dev/null || true
    fi
    rm -f "$_PF_PIDFILE"
fi
rm -f /tmp/vcluster-isv-lb-ip.txt

echo "Deleting vCluster '${VCLUSTER_NAME}' from namespace '${VCLUSTER_NAMESPACE}'..." >&2

DELETE_RC=0
DELETE_OUT=$(vcluster delete "$VCLUSTER_NAME" --namespace "$VCLUSTER_NAMESPACE" 2>&1) || DELETE_RC=$?

if [ "$DELETE_RC" -ne 0 ]; then
    if echo "$DELETE_OUT" | grep -qi "not found\|does not exist\|couldn't find"; then
        echo "vCluster '${VCLUSTER_NAME}' was already absent; nothing to do." >&2
    else
        echo "Error: vcluster delete failed (exit ${DELETE_RC}):" >&2
        echo "$DELETE_OUT" >&2
        exit 1
    fi
else
    echo "$DELETE_OUT" >&2
    echo "vCluster deleted." >&2
fi

# Remove the persisted kubeconfig
if [ -f "$VCLUSTER_KUBECONFIG_PATH" ]; then
    rm -f "$VCLUSTER_KUBECONFIG_PATH"
    echo "Removed kubeconfig at ${VCLUSTER_KUBECONFIG_PATH}." >&2
fi

echo "Teardown complete." >&2
