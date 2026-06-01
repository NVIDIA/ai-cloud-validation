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

"""Slurm GPU stress workload.

This module runs a GPU stress test (matrix multiplications) on all nodes in a
Slurm partition to verify that each node can execute a serious GPU workload.

This ensures that all nodes are able to run a serious computation job using
the GPUs.
"""

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from isvtest.config.settings import (
    get_gpu_cuda_arch,
    get_gpu_memory_gb,
    get_gpu_stress_image,
    get_gpu_stress_runtime,
    get_gpu_stress_timeout,
)
from isvtest.core.slurm import detect_container_runtime, get_partition_nodes, is_gpu_partition
from isvtest.core.workload import BaseWorkloadCheck

SCRIPTS_DIR = Path(__file__).parent / "scripts"


@dataclass
class NodeStressResult:
    """Result of stress testing a single node."""

    node: str
    success: bool
    gpu_count: int = 0
    loops_completed: int = 0
    error: str = ""


class SlurmGpuStressWorkload(BaseWorkloadCheck):
    """Run GPU stress test on all nodes in a Slurm partition.

    Config options:
        partition (str): Partition to test (default: "gpu")
        runtime (int): Stress test runtime in seconds (default: 30 via get_gpu_stress_runtime)
        memory_gb (int): Target GPU memory in GB (default: 32 via get_gpu_memory_gb)
        timeout (int): Per-node timeout in seconds (default: 420 via get_gpu_stress_timeout)
        container_runtime (str): "enroot" | "pyxis" | "singularity" | "docker" | "python"
            (default: auto-detect, enroot > singularity > docker).
            Set "python" to run directly with system Python (requires PyTorch
            pre-installed on compute nodes).
        image (str): Container image (default: nvcr.io/nvidia/pytorch:25.04-py3)
        cuda_arch (str): CUDA compute capability (e.g., "100" for GB200)
        num_gpus (int): GPUs per node. None yields --gres=gpu (Slurm default, commonly
            1 GPU per task); specify an integer to request multiple GPUs explicitly.
    """

    description: ClassVar[str] = "Run GPU stress test on all Slurm nodes"
    timeout: ClassVar[int] = 1800
    labels: ClassVar[tuple[str, ...]] = ("workload", "slurm", "gpu", "slow")
    test_ids: ClassVar[tuple[str, ...]] = ("SLURM-XX-07",)

    def run(self) -> None:
        """Execute GPU stress test on all nodes in the partition."""
        partition = self.config.get("partition", "gpu")
        runtime_config = self.config.get("runtime")
        runtime = runtime_config if runtime_config is not None else get_gpu_stress_runtime()
        memory_config = self.config.get("memory_gb")
        memory_gb = memory_config if memory_config is not None else get_gpu_memory_gb()
        timeout_config = self.config.get("timeout")
        node_timeout = timeout_config if timeout_config is not None else get_gpu_stress_timeout()
        image_config = self.config.get("image")
        image = image_config if image_config is not None else get_gpu_stress_image()
        cuda_arch_config = self.config.get("cuda_arch")
        cuda_arch = cuda_arch_config if cuda_arch_config is not None else get_gpu_cuda_arch()
        num_gpus = self.config.get("num_gpus")
        container_runtime = self.config.get("container_runtime")

        exec_mode = self._resolve_execution_mode(container_runtime)
        self.log.info(f"Using execution mode: {exec_mode}")

        if not is_gpu_partition(self, partition):
            self.set_failed(f"Partition '{partition}' is not a GPU partition")
            return

        nodes = get_partition_nodes(self, partition)
        if nodes is None:
            return
        if not nodes:
            self.set_failed(f"No nodes found in partition '{partition}'")
            return

        self.log.info(f"Testing {len(nodes)} nodes in parallel: runtime={runtime}s, memory_gb={memory_gb}")

        env_vars = f"GPU_STRESS_RUNTIME={runtime} GPU_MEMORY_GB={memory_gb}"
        if cuda_arch:
            env_vars += f" CUPY_CUDA_ARCH_LIST={cuda_arch}"

        exec_cmd, display_cmd, srun_extra = self._build_command(exec_mode, image, env_vars)
        results = self._run_on_all_nodes(nodes, partition, num_gpus, node_timeout, exec_cmd, display_cmd, srun_extra)
        self._report_results(results)

    def _resolve_execution_mode(self, container_runtime: str | None = None) -> str:
        """Resolve execution mode from ``container_runtime`` config value.

        Args:
            container_runtime: Explicit runtime ("enroot", "pyxis",
                "singularity", "docker", or "python"). ``None`` triggers
                auto-detection via ``detect_container_runtime()``
                (enroot > singularity > docker).

        Returns:
            ``"python"`` or ``"container:<runtime>"``.
        """
        if container_runtime == "python":
            return "python"
        rt = container_runtime or detect_container_runtime(self)
        return f"container:{rt}"

    def _build_command(self, exec_mode: str, image: str, env_vars: str) -> tuple[str, str, str]:
        """Build the execution command.

        Uses base64 encoding to safely pass multiline Python scripts through shell.

        Returns:
            Tuple of (actual_command, display_command, srun_extra_opts) where
            display_command is human-readable for logging and srun_extra_opts
            contains additional srun flags (e.g. ``--container-image`` for enroot/pyxis).
        """
        script_path = SCRIPTS_DIR / "gpu_stress_torch.py"
        script_content = script_path.read_text()
        script_b64 = base64.b64encode(script_content.encode()).decode()

        # Decode and execute: echo <b64> | base64 -d | python3
        python_cmd = f"echo {script_b64} | base64 -d | python3"
        display_python_cmd = f"python3 {script_path.name}"

        if exec_mode.startswith("container:"):
            runtime = exec_mode.split(":", 1)[1]
            if runtime in ("enroot", "pyxis"):
                # Enroot (via pyxis): container is managed by srun via --container-image
                actual = f"bash -c '{env_vars} {python_cmd}'"
                display = f"{env_vars} {display_python_cmd}"
                return actual, display, f"--container-image={image}"
            elif runtime == "docker":
                env_parts = " ".join(f"-e {v}" for v in env_vars.split())
                actual = f"docker run --rm --gpus all {env_parts} {image} bash -c '{python_cmd}'"
                display = f"docker run --rm --gpus all {env_parts} {image} {display_python_cmd}"
                return actual, display, ""
            else:  # singularity
                actual = f"{runtime} exec --nv docker://{image} bash -c '{env_vars} {python_cmd}'"
                display = f"{runtime} exec --nv docker://{image} {env_vars} {display_python_cmd}"
                return actual, display, ""
        else:  # python
            actual = f"bash -c '{env_vars} {python_cmd}'"
            display = f"{env_vars} {display_python_cmd}"
            return actual, display, ""

    def _run_on_all_nodes(
        self,
        nodes: list[str],
        partition: str,
        num_gpus: int | None,
        timeout: int,
        exec_cmd: str,
        display_cmd: str,
        srun_extra: str = "",
    ) -> list[NodeStressResult]:
        """Run stress test on all nodes simultaneously."""
        gres_opt = f"--gres=gpu:{num_gpus}" if num_gpus else "--gres=gpu"
        nodelist = ",".join(nodes)

        # Use a unique job name so we can cancel orphaned jobs on timeout
        job_name = f"isvtest-gpu-stress-{os.getpid()}"

        srun_base = (
            f"srun --job-name={job_name} --partition={partition} --nodes={len(nodes)} "
            f"--nodelist={nodelist} --ntasks={len(nodes)} --ntasks-per-node=1 {gres_opt} "
            f"--chdir=/tmp --label"
        )
        if srun_extra:
            srun_base = f"{srun_base} {srun_extra}"

        srun_cmd = f"{srun_base} {exec_cmd}"
        srun_display = f"{srun_base} {display_cmd}"
        result = self.run_command(srun_cmd, timeout=timeout, display_cmd=srun_display)

        # If command timed out, cancel any orphaned jobs
        if result.timed_out:
            self.log.warning(f"srun timed out after {timeout}s, cancelling orphaned jobs...")
            self._cancel_jobs_by_name(job_name)

        output = f"{result.stdout}\n{result.stderr}".strip()

        # Log raw output for debugging parsing issues
        if output:
            self.log.debug(f"Raw srun output ({len(output)} chars):\n{output[:2000]}")
        else:
            self.log.warning("No output received from srun command")

        return self._parse_output(nodes, output)

    def _cancel_jobs_by_name(self, job_name: str) -> None:
        """Cancel any jobs matching the given name.

        Args:
            job_name: The Slurm job name to cancel.
        """
        # Find job IDs by name
        result = self.run_command(
            f"squeue --name={job_name} --noheader --format='%i'",
            timeout=30,
        )
        if result.exit_code == 0 and result.stdout.strip():
            job_ids = result.stdout.strip().split()
            for job_id in job_ids:
                self.log.info(f"Cancelling orphaned job {job_id}")
                self.run_command(f"scancel {job_id}", timeout=30)
        elif result.exit_code != 0:
            self.log.warning(
                f"Failed to query squeue for job_name={job_name} (exit={result.exit_code}): {result.stderr}"
            )

    def _parse_output(self, nodes: list[str], output: str) -> list[NodeStressResult]:
        """Parse labeled srun output to get per-node results."""
        node_outputs: dict[str, list[str]] = {node: [] for node in nodes}

        for line in output.splitlines():
            if ": " in line:
                task_id_str, content = line.split(": ", 1)
                try:
                    task_id = int(task_id_str.strip())
                    if 0 <= task_id < len(nodes):
                        node_outputs[nodes[task_id]].append(content)
                except ValueError:
                    continue

        return [self._parse_node_output(node, "\n".join(node_outputs[node])) for node in nodes]

    def _parse_node_output(self, node: str, output: str) -> NodeStressResult:
        """Parse single node output."""
        if match := re.search(r"(FAILURE:.*)", output):
            return NodeStressResult(node=node, success=False, error=match.group(1).strip())
        if match := re.search(r"SUCCESS:.*completed (\d+) loops with (\d+) GPU", output):
            loops, gpus = int(match.group(1)), int(match.group(2))
            self.log.info(f"Node {node}: SUCCESS - {loops} loops, {gpus} GPU(s)")
            return NodeStressResult(node=node, success=True, gpu_count=gpus, loops_completed=loops)
        return NodeStressResult(node=node, success=False, error="SUCCESS marker not found")

    def _report_results(self, results: list[NodeStressResult]) -> None:
        """Report test results."""
        passed = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        details = "\n".join(
            f"  [PASS] {r.node}: {r.loops_completed} loops, {r.gpu_count} GPU(s)"
            if r.success
            else f"  [FAIL] {r.node}: {r.error}"
            for r in results
        )

        if failed:
            self.set_failed(f"Failed on {len(failed)}/{len(results)} nodes:\n{details}", output=details)
        else:
            total = sum(r.gpu_count for r in passed)
            self.set_passed(f"Passed on {len(passed)} nodes ({total} GPUs):\n{details}")
