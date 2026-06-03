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

"""Slurm node job execution validations.

This module verifies that all nodes in a partition can actually run SLURM jobs
by executing a test job on each node individually.
"""

from dataclasses import dataclass
from typing import ClassVar

import pytest

from isvtest.core.nvidia import extract_first_gpu_info, has_gpu_output
from isvtest.core.slurm import (
    DEFAULT_NODE_TIMEOUT,
    MANIFESTS_DIR,
    get_partition_names,
    get_partition_nodes,
    is_gpu_partition,
)
from isvtest.core.validation import BaseValidation


@dataclass
class NodeTestResult:
    """Result of testing a single node."""

    node: str
    success: bool
    info: str = ""  # GPU or CPU info
    storage_ok: bool = False
    compute_ok: bool = False
    compute_skipped: bool = False
    error: str = ""


class SlurmNodeJobExecution(BaseValidation):
    """Verify that all nodes in a partition can execute SLURM jobs.

    This test auto-discovers nodes from the specified partition and runs
    a test job on each node to verify:
    - GPU access (for GPU partitions): nvidia-smi availability and GPU detection
    - Storage access: ability to write/read files
    - Basic computation: GPU memory allocation (GPU nodes) or CPU math (CPU nodes)
    - Basic job execution capability

    Config options:
        partition_name (str): Partition to test (required)
        storage_path (str): Path for storage test (default: /tmp)
        timeout_per_node (int): Timeout in seconds per node (default: 60)
        test_compute (bool): Enable computation test (default: True)
    """

    description: ClassVar[str] = "Verify all nodes in partition can execute SLURM jobs"
    timeout: ClassVar[int] = 300

    def run(self) -> None:
        """Execute job tests on all nodes in the partition."""
        partition_name = self.config.get("partition_name")
        if not partition_name:
            self.set_failed("partition_name is required")
            return

        storage_path = self.config.get("storage_path", "/tmp")
        timeout_per_node = self.config.get("timeout_per_node", DEFAULT_NODE_TIMEOUT)
        test_compute = self.config.get("test_compute", True)

        # Get nodes in partition
        nodes = get_partition_nodes(self, partition_name)
        if nodes is None:
            return  # Error already set
        if not nodes:
            result = self.run_command("sinfo -o '%P %a %l %D %N'")
            available = get_partition_names(result.stdout) if result.exit_code == 0 else []
            pytest.skip(f"Partition '{partition_name}' has no nodes (available partitions: {available})")

        self.log.info(f"Found {len(nodes)} nodes in partition '{partition_name}': {nodes}")

        # Determine GPU vs CPU partition
        is_gpu = is_gpu_partition(self, partition_name)

        # Test each node
        results = [
            self._test_node(node, partition_name, is_gpu, storage_path, timeout_per_node, test_compute)
            for node in nodes
        ]

        self._report_results(results, partition_name)

    def _test_node(
        self,
        node: str,
        partition: str,
        is_gpu: bool,
        storage_path: str,
        timeout: int,
        test_compute: bool,
    ) -> NodeTestResult:
        """Run test job on a specific node."""
        self.log.info(f"Testing node: {node}")

        # Load appropriate test script
        script_name = "gpu_node_test.sh" if is_gpu else "cpu_node_test.sh"
        script_path = MANIFESTS_DIR / script_name

        if not script_path.exists():
            return NodeTestResult(node=node, success=False, error=f"Script not found: {script_path}")

        script = script_path.read_text()

        # For GPU scripts, inject the CUDA source from the standalone .cu file
        if is_gpu and "{{GPU_COMPUTE_SOURCE}}" in script:
            cuda_path = MANIFESTS_DIR / "gpu_compute_test.cu"
            if cuda_path.exists():
                script = script.replace("{{GPU_COMPUTE_SOURCE}}", cuda_path.read_text().strip())

        # Build srun command with environment variables
        env_vars = f"STORAGE_PATH={storage_path} TEST_COMPUTE={'true' if test_compute else 'false'}"
        gres_opt = "--gres=gpu:1" if is_gpu else ""

        result = self.run_command(
            f"srun --nodelist={node} --partition={partition} {gres_opt} --chdir=/tmp -N1 "
            f"bash -c '{env_vars} bash -s' << 'SCRIPT_EOF'\n{script}\nSCRIPT_EOF",
            timeout=timeout,
        )

        if result.exit_code != 0:
            return NodeTestResult(
                node=node,
                success=False,
                error=f"srun failed: {result.stderr.strip() or result.stdout.strip()}",
            )

        return self._parse_output(node, result.stdout, is_gpu, test_compute)

    def _parse_output(
        self,
        node: str,
        output: str,
        is_gpu: bool,
        test_compute: bool,
    ) -> NodeTestResult:
        """Parse test script output and build result."""
        self.log.debug(f"Node {node} output: {output}")

        storage_ok = "STORAGE_OK" in output
        compute_skipped = "GPU_COMPUTE_SKIPPED" in output or "CPU_COMPUTE_SKIPPED" in output
        compute_ok = "GPU_COMPUTE_OK" in output or "CPU_COMPUTE_OK" in output or not test_compute

        if is_gpu:
            if not has_gpu_output(output):
                return NodeTestResult(
                    node=node,
                    success=False,
                    error=f"GPU test failed - no GPU detected. Output: {output[:200]}",
                )
            info = extract_first_gpu_info(output)
        else:
            info = self._parse_cpu_info(output)

        if not storage_ok:
            return NodeTestResult(
                node=node,
                success=False,
                info=info,
                storage_ok=False,
                compute_ok=compute_ok,
                compute_skipped=compute_skipped,
                error="Storage test failed",
            )

        if test_compute and not compute_ok and not compute_skipped:
            return NodeTestResult(
                node=node,
                success=False,
                info=info,
                storage_ok=storage_ok,
                compute_ok=False,
                error="Computation test failed",
            )

        return NodeTestResult(
            node=node,
            success=True,
            info=info,
            storage_ok=storage_ok,
            compute_ok=compute_ok,
            compute_skipped=compute_skipped,
        )

    def _parse_cpu_info(self, output: str) -> str:
        """Extract CPU info from output."""
        in_section = False
        for line in output.split("\n"):
            line = line.strip()
            if "CPU_INFO_START" in line:
                in_section = True
                continue
            if "CPU_INFO_END" in line:
                break
            if in_section and line.isdigit():
                return f"CPUs: {line}"
        return "CPU info available"

    def _report_results(self, results: list[NodeTestResult], partition_name: str) -> None:
        """Aggregate and report results."""
        passed = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        details = []
        for r in results:
            if r.success:
                parts = [r.info or "OK"]
                if r.storage_ok:
                    parts.append("Storage=OK")
                if r.compute_ok:
                    parts.append("Compute=OK")
                details.append(f"  [PASS] {r.node}: {', '.join(parts)}")
            else:
                details.append(f"  [FAIL] {r.node}: {r.error}")

        detail_str = "\n".join(details)

        if failed:
            self.set_failed(
                f"{len(failed)}/{len(results)} nodes failed in partition '{partition_name}':\n{detail_str}",
                output=detail_str,
            )
        else:
            self.set_passed(f"All {len(passed)} nodes in partition '{partition_name}' passed:\n{detail_str}")
