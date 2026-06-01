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

import uuid
from pathlib import Path
from typing import ClassVar

from isvtest.config.settings import (
    get_k8s_namespace,
    get_nccl_gpu_count,
    get_nccl_min_bus_bw_gbps,
    get_nccl_timeout,
)
from isvtest.core.k8s import get_gpu_nodes, get_node_gpu_count
from isvtest.core.workload import BaseWorkloadCheck
from isvtest.workloads.nccl_common import parse_nccl_output


class K8sNcclWorkload(BaseWorkloadCheck):
    """Run NCCL allreduce test on Kubernetes.

    Config:
        min_bus_bw_gbps (float): Minimum expected bus bandwidth in GB/s (default: env or 0 = no check)
    """

    description = "Run NCCL allreduce test on Kubernetes."
    labels: ClassVar[tuple[str, ...]] = ("workload", "kubernetes", "gpu", "slow")
    test_ids: ClassVar[tuple[str, ...]] = ("K8S-XX-05",)

    def run(self) -> None:
        # Get configuration
        namespace = get_k8s_namespace()
        timeout = self.config.get("timeout") or get_nccl_timeout()

        min_bus_bw_config = self.config.get("min_bus_bw_gbps")
        min_bus_bw = float(min_bus_bw_config) if min_bus_bw_config is not None else get_nccl_min_bus_bw_gbps()

        # Verify GPU nodes available
        # Note: We still rely on k8s_utils here for convenience, but we should eventually move this to Runner
        nodes = get_gpu_nodes()
        if not nodes:
            self.set_passed("Skipped: No GPU nodes found in cluster")
            return

        # Determine GPU count
        configured_gpu_count = get_nccl_gpu_count()
        if configured_gpu_count is not None:
            gpu_count = configured_gpu_count
        else:
            # Auto-detect from first node
            gpu_count = get_node_gpu_count(nodes[0])
            if gpu_count == 0:
                self.set_failed(f"Could not determine GPU count for node {nodes[0]}")
                return

        # NCCL tests need at least 2 GPUs for meaningful results
        if gpu_count < 2:
            self.set_passed(f"Skipped: Node has only {gpu_count} GPU(s), need at least 2 for NCCL allreduce test")
            return

        # Generate unique job name
        job_name = f"nccl-allreduce-gpu-{uuid.uuid4().hex[:8]}"

        # Get path to YAML file and read it
        manifest_path = Path(__file__).parent / "manifests" / "k8s" / "nccl_allreduce_job.yaml"

        if not manifest_path.exists():
            self.set_failed(f"Manifest file not found: {manifest_path}")
            return

        yaml_content = manifest_path.read_text()

        # Replace job name and GPU count to match available resources
        yaml_content = yaml_content.replace("name: nccl-allreduce-gpu", f"name: {job_name}", 1)
        yaml_content = yaml_content.replace("nvidia.com/gpu: 8", f"nvidia.com/gpu: {gpu_count}")
        yaml_content = yaml_content.replace("-np 8", f"-np {gpu_count}")

        self.log.info(f"Starting NCCL test with {gpu_count} GPUs (timeout: {timeout}s)")

        # Run the job using the helper
        result = self.run_k8s_job(job_name=job_name, namespace=namespace, yaml_content=yaml_content, timeout=timeout)

        if result.exit_code != 0:
            self.set_failed(f"NCCL test failed: {result.stderr}")
            return

        nccl = parse_nccl_output(result.stdout)

        if not nccl.success:
            self.set_failed(nccl.error, output=nccl.output)
            return

        if min_bus_bw > 0 and nccl.avg_bus_bw_gbps < min_bus_bw:
            self.set_failed(f"Bus bandwidth {nccl.avg_bus_bw_gbps:.2f} GB/s below minimum {min_bus_bw} GB/s")
            return

        msg = "NCCL allreduce test passed\n"
        msg += f"Average Bus Bandwidth: {nccl.avg_bus_bw_gbps:.2f} GB/s\n"
        if nccl.out_of_bounds >= 0:
            msg += f"Out of Bounds Values: {nccl.out_of_bounds} (Pass)\n"

        self.set_passed(msg)
