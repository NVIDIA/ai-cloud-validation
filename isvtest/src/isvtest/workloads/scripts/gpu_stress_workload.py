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

# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cupy-cuda12x>=13.6.0",  # Auto-selects CUDA version based on system
# ]
# ///
"""GPU stress test workload using CuPy.

This module provides a GPU stress test that:
- Fills GPU memory with large tensors
- Performs intensive matrix multiplications
- Tests GPU-to-GPU communication
- Validates computation correctness

Adapted from dgxc-cluster-validator for isvtest framework.

Can be run directly with: uv run gpu_stress_workload.py
"""

import math
import os
import socket
import time

import cupy as cp

# Configuration from environment
GPU_MEMORY_IN_GB = int(os.getenv("GPU_MEMORY_GB", "32"))  # Default 32GB
MAX_RUNTIME = int(os.getenv("GPU_STRESS_RUNTIME", "30"))  # Default 30 seconds
CUDA_ARCH = os.getenv("CUPY_CUDA_ARCH_LIST", "NOT SET")

print(f"DEBUG: CUPY_CUDA_ARCH_LIST={CUDA_ARCH}")
print(
    f"DEBUG: All CUDA-related env vars: {[(k, v) for k, v in os.environ.items() if 'CUDA' in k.upper() or 'CUPY' in k.upper()]}"
)


def run_gpu_stress() -> str:
    """Run GPU stress test on all available GPUs.

    Returns:
        Status message indicating success or failure.
    """
    hostname = socket.gethostname()

    # Check CUDA availability
    try:
        num_gpus = cp.cuda.runtime.getDeviceCount()
    except Exception:
        return f"FAILURE: CUDA is not available on {hostname}"

    if num_gpus == 0:
        return f"FAILURE: No GPUs detected on {hostname}"

    print(f"{hostname}: Starting GPU stress test with {num_gpus} GPU(s)")
    print(f"{hostname}: Max runtime: {MAX_RUNTIME} seconds")
    print(f"{hostname}: GPU memory target: {GPU_MEMORY_IN_GB} GB per GPU")

    # Get the array size for a square array that fills 1/4 of memory with 2 byte values
    arr_size = (((GPU_MEMORY_IN_GB / 4) * 10**9) / 2) ** (1 / 2)
    arr_size = math.ceil(arr_size)

    print(f"{hostname}: Array size: {arr_size}x{arr_size} (float16)")

    try:
        # Allocate arrays on all GPUs
        print(f"{hostname}: Allocating arrays on {num_gpus} GPU(s)...")
        Ts = []
        results = []
        from_others = []

        for gpu_num in range(num_gpus):
            with cp.cuda.Device(gpu_num):
                Ts.append(cp.ones((arr_size, arr_size), dtype=cp.float16))
                results.append(cp.zeros((arr_size, arr_size), dtype=cp.float16))
                from_others.append(cp.zeros((arr_size, arr_size), dtype=cp.float16))

        cp.random.seed(12345)

        # Sync to ensure allocation is complete before timing
        for gpu_num in range(num_gpus):
            with cp.cuda.Device(gpu_num):
                cp.cuda.runtime.deviceSynchronize()

        start_time = time.time()
        curr_loop_num = 0

        print(f"{hostname}: Starting computation loop...")
        while time.time() - start_time < MAX_RUNTIME:
            # Matrix multiply into result
            for gpu_num, (T, result) in enumerate(zip(Ts, results)):
                with cp.cuda.Device(gpu_num):
                    cp.matmul(T, T, out=result)

            # Move data to different GPU (GPU-to-GPU transfer test)
            if num_gpus > 1:
                for i in range(num_gpus):
                    other_gpu = (curr_loop_num % (num_gpus - 1) + i + 1) % num_gpus
                    other = from_others[other_gpu]
                    original = results[i]

                    with cp.cuda.Device(other_gpu):
                        other[:] = cp.asarray(original)

                # Check values are correct
                checks = []
                for gpu_num, (other, result) in enumerate(zip(from_others, results)):
                    with cp.cuda.Device(gpu_num):
                        check = (other == result).sum() == result.size
                        checks.append(bool(check))

                if not all(checks):
                    return f"FAILURE: GPU validation error - values don't match on {hostname}"

            curr_loop_num += 1

            # Progress reporting every 10 loops (with sync for accurate timing)
            if curr_loop_num % 10 == 0:
                cp.cuda.runtime.deviceSynchronize()  # Wait for GPU to catch up
                elapsed = time.time() - start_time
                print(f"{hostname}: Loop {curr_loop_num}, elapsed: {elapsed:.1f}s")

        elapsed_time = time.time() - start_time
        print(f"{hostname}: Completed {curr_loop_num} loops in {elapsed_time:.1f} seconds")

        # Sanity check: ensure we did reasonable number of loops
        if curr_loop_num < num_gpus:
            return f"FAILURE: Too few loops completed ({curr_loop_num}) on {hostname}"

        # Explicit cleanup to prevent hanging on exit
        cp.get_default_memory_pool().free_all_blocks()
        cp.cuda.runtime.deviceSynchronize()

        return f"SUCCESS: {hostname} completed {curr_loop_num} loops with {num_gpus} GPU(s)"

    except cp.cuda.memory.OutOfMemoryError as e:
        return f"FAILURE: GPU out of memory on {hostname}: {e}"
    except Exception as e:
        return f"FAILURE: Unexpected error on {hostname}: {e}"


if __name__ == "__main__":
    hostname = socket.gethostname()
    try:
        result = run_gpu_stress()
        print(f"\n{'=' * 60}")
        print(result)
        print(f"{'=' * 60}")

        # Exit with appropriate code
        if result.startswith("SUCCESS"):
            exit(0)
        else:
            exit(1)
    except Exception as e:
        print(f"\n{'=' * 60}")
        print(f"FAILURE: Critical error on {hostname}: {e}")
        print(f"{'=' * 60}")
        exit(1)
