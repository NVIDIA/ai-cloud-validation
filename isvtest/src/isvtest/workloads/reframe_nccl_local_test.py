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

"""NCCL AllReduce local test using Docker/Singularity directly.

This is a development/standalone version of the NCCL test that can run
on systems without Slurm, using Docker or Singularity directly.

For production Slurm environments, use reframe_nccl_tests.py instead.
"""

import shutil
from typing import Any, ClassVar

import reframe as rfm
import reframe.utility.sanity as sn
from reframe.core.builtins import performance_function, run_after, sanity_function


@rfm.simple_test
class NCCLAllReduceLocalTest(rfm.RunOnlyRegressionTest):
    """NCCL AllReduce test for local/dev systems using containers directly."""

    descr = "NCCL AllReduce local test with Docker/Singularity"
    valid_systems: ClassVar[list[str]] = ["*"]
    valid_prog_environs: ClassVar[list[str]] = ["*"]

    @run_after("init")
    def set_tags(self) -> None:
        """Set test tags."""
        self.tags = {"nccl", "workload", "hpc", "network", "performance", "local"}

    @run_after("init")
    def detect_container_runtime(self) -> None:
        """Detect and configure available container runtime."""
        # Check for Singularity
        if shutil.which("singularity"):
            self.executable = "singularity"
            self.executable_opts = [
                "exec",
                "--nv",  # Enable NVIDIA GPU support
                "docker://nvcr.io/nvidia/hpc-benchmarks:25.04",
                "all_reduce_perf",
                "-b",
                "8",
                "-e",
                "4G",
                "-f",
                "2",
                "-g",
                "4",
            ]
        # Check for Docker
        elif shutil.which("docker"):
            self.executable = "docker"
            self.executable_opts = [
                "run",
                "--rm",
                "--gpus",
                "all",
                "--ipc=host",  # Required for NCCL shared memory
                "--ulimit",
                "memlock=-1",  # Required for pinned memory
                "--ulimit",
                "stack=67108864",  # 64MB stack size
                "nvcr.io/nvidia/hpc-benchmarks:25.04",
                "all_reduce_perf",
                "-b",
                "8",
                "-e",
                "4G",
                "-f",
                "2",
                "-g",
                "1",  # Use 1 GPU for local testing (adjust as needed)
            ]
        # Check for Enroot
        elif shutil.which("enroot"):
            self.executable = "enroot"
            self.executable_opts = [
                "start",
                "--rw",
                "nvcr.io#nvidia#hpc-benchmarks:25.04",
                "all_reduce_perf",
                "-b",
                "8",
                "-e",
                "4G",
                "-f",
                "2",
                "-g",
                "4",
            ]
        else:
            # Skip test if no container runtime found
            self.skip_if(
                True,
                "No container runtime found. Install singularity, docker, or enroot.",
            )

    @sanity_function
    def assert_nccl_success(self) -> Any:
        """Verify NCCL test completed successfully."""
        return sn.assert_ge(self.avg_bus_bandwidth(), 0)

    @performance_function("GB/s")
    def avg_bus_bandwidth(self) -> Any:
        """Extract average bus bandwidth performance metric.

        Returns:
            Average bus bandwidth in GB/s.
        """
        pattern = r"# Avg bus bandwidth\s*:\s*([\d.]+)"
        return sn.extractsingle(pattern, self.stdout, 1, float)
