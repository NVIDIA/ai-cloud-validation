#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

# Pre-Test GPU Resource Cleanup
#
# Deletes Completed/Failed pods that permanently hold nvidia.com/gpu resources,
# which would prevent workload tests (NCCL, GPU stress, NIM) from scheduling.
#
# Known offenders on zCompute EKS-D:
#   - nvidia-cuda-validator-* (zadara-system): GPU Operator validation pods,
#     request 1 GPU each, remain Completed forever unless deleted.
#   - Any stale isvtest-* pods from interrupted previous runs.
#
# This script is idempotent — safe to run multiple times.

set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"

echo "=== Pre-test GPU resource cleanup ==="

# --- 1. Delete GPU Operator nvidia-cuda-validator Completed pods ---
# These run once at GPU Operator init, complete, and are never garbage-collected
# by the Operator. Each holds 1 nvidia.com/gpu permanently.
echo "Cleaning up nvidia-cuda-validator Completed pods..."
VALIDATOR_PODS=$($KUBECTL get pods -n zadara-system \
    -l app=nvidia-cuda-validator \
    --field-selector=status.phase=Succeeded \
    -o name 2>/dev/null || true)

if [ -n "$VALIDATOR_PODS" ]; then
    echo "$VALIDATOR_PODS" | xargs $KUBECTL delete -n zadara-system --grace-period=0 --force 2>/dev/null || true
    echo "  Deleted: $VALIDATOR_PODS"
else
    # Fall back to label-less delete of any Completed pods in zadara-system
    $KUBECTL delete pods -n zadara-system \
        --field-selector=status.phase=Succeeded \
        --grace-period=0 --force 2>/dev/null || true
    echo "  No nvidia-cuda-validator Completed pods found (or already cleaned up)"
fi

# --- 2. Delete any stale isvtest-* GPU pods from previous/interrupted runs ---
echo "Cleaning up stale isvtest-* pods in default namespace..."
$KUBECTL delete pods -n default \
    --field-selector=status.phase=Succeeded \
    --grace-period=0 --force 2>/dev/null || true
$KUBECTL delete pods -n default \
    --field-selector=status.phase=Failed \
    --grace-period=0 --force 2>/dev/null || true

# --- 3. Delete any stale NCCL / NIM / stress pods from interrupted runs ---
echo "Cleaning up stale GPU workload pods..."
for label in "app=nccl-test" "app=gpu-stress-test" "app=nim-inference-test"; do
    $KUBECTL delete pods -n default -l "$label" --grace-period=0 --force 2>/dev/null || true
done

# Also clean up any orphaned MPIJobs (NCCL multi-node) and Jobs
$KUBECTL delete mpijobs -n default --all 2>/dev/null || true
$KUBECTL delete jobs -n default -l app=nccl-test 2>/dev/null || true
$KUBECTL delete jobs -n default -l app=nim-inference-test 2>/dev/null || true

# --- 4. Verify GPU resources are now free ---
echo ""
echo "=== GPU allocation after cleanup ==="
$KUBECTL get nodes -o custom-columns=\
"NAME:.metadata.name,\
GPU-ALLOC:.status.allocatable.nvidia\.com/gpu,\
GPU-CAP:.status.capacity.nvidia\.com/gpu" \
    2>/dev/null || true

echo ""
echo "=== Pre-test cleanup complete ==="
echo '{"success": true}'
