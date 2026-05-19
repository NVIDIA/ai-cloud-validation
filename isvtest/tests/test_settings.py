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

"""Tests for settings module."""

import os
from unittest.mock import patch

from isvtest.config.settings import (
    get_gpu_cuda_arch,
    get_gpu_memory_gb,
    get_gpu_stress_gpu_count,
    get_gpu_stress_image,
    get_gpu_stress_runtime,
    get_gpu_stress_timeout,
    get_k8s_gpu_operator_namespace,
    get_k8s_namespace,
    get_nccl_gpu_count,
    get_nccl_hpc_image,
    get_nccl_image,
    get_nccl_min_bus_bw_gbps,
    get_nccl_multinode_gpus_per_node,
    get_nccl_multinode_nodes,
    get_nccl_multinode_timeout,
    get_nccl_results_hostpath,
    get_nccl_timeout,
    get_nim_genai_perf_concurrency,
    get_nim_genai_perf_requests,
    get_nim_helm_chart_version,
    get_nim_helm_gpu_count,
    get_nim_helm_model,
    get_nim_helm_model_tag,
    get_nim_helm_timeout,
)


class TestK8sSettings:
    """Tests for Kubernetes settings functions."""

    def test_get_k8s_gpu_operator_namespace_default(self) -> None:
        """Test default GPU operator namespace."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_k8s_gpu_operator_namespace()
            assert result == "nvidia-gpu-operator"

    def test_get_k8s_gpu_operator_namespace_override(self) -> None:
        """Test explicit namespace override."""
        with patch.dict(os.environ, {"K8S_GPU_OPERATOR_NAMESPACE": "custom-ns"}):
            result = get_k8s_gpu_operator_namespace()
            assert result == "custom-ns"

    def test_get_k8s_gpu_operator_namespace_microk8s(self) -> None:
        """Test microk8s provider uses different namespace."""
        with patch.dict(os.environ, {"K8S_PROVIDER": "microk8s"}, clear=True):
            result = get_k8s_gpu_operator_namespace()
            assert result == "gpu-operator-resources"

    def test_get_k8s_namespace_default(self) -> None:
        """Test default Kubernetes namespace."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_k8s_namespace()
            assert result == "default"

    def test_get_k8s_namespace_custom(self) -> None:
        """Test custom Kubernetes namespace."""
        with patch.dict(os.environ, {"K8S_NAMESPACE": "test-ns"}):
            result = get_k8s_namespace()
            assert result == "test-ns"


class TestGpuStressSettings:
    """Tests for GPU stress test settings."""

    def test_get_gpu_stress_image_default(self) -> None:
        """Test default GPU stress image."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_gpu_stress_image()
            assert "pytorch" in result

    def test_get_gpu_stress_image_custom(self) -> None:
        """Test custom GPU stress image."""
        with patch.dict(os.environ, {"GPU_STRESS_IMAGE": "custom/image:tag"}):
            result = get_gpu_stress_image()
            assert result == "custom/image:tag"

    def test_get_gpu_stress_runtime_default(self) -> None:
        """Test default GPU stress runtime."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_gpu_stress_runtime()
            assert result == 30

    def test_get_gpu_stress_runtime_custom(self) -> None:
        """Test custom GPU stress runtime."""
        with patch.dict(os.environ, {"GPU_STRESS_RUNTIME": "120"}):
            result = get_gpu_stress_runtime()
            assert result == 120

    def test_get_gpu_stress_timeout_default(self) -> None:
        """Test default GPU stress timeout."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_gpu_stress_timeout()
            assert result == 420

    def test_get_gpu_stress_gpu_count_default(self) -> None:
        """Test default GPU stress GPU count is None (auto-detect)."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_gpu_stress_gpu_count()
            assert result is None

    def test_get_gpu_stress_gpu_count_custom(self) -> None:
        """Test custom GPU stress GPU count."""
        with patch.dict(os.environ, {"GPU_STRESS_GPU_COUNT": "4"}):
            result = get_gpu_stress_gpu_count()
            assert result == 4

    def test_get_gpu_stress_gpu_count_zero(self) -> None:
        """Test GPU stress GPU count explicitly set to zero returns 0, not None."""
        with patch.dict(os.environ, {"GPU_STRESS_GPU_COUNT": "0"}):
            result = get_gpu_stress_gpu_count()
            assert result == 0

    def test_get_gpu_memory_gb_default(self) -> None:
        """Test default GPU memory size."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_gpu_memory_gb()
            assert result == 32

    def test_get_gpu_cuda_arch_default(self) -> None:
        """Test default CUDA arch is None (auto-detect)."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_gpu_cuda_arch()
            assert result is None

    def test_get_gpu_cuda_arch_custom(self) -> None:
        """Test custom CUDA arch."""
        with patch.dict(os.environ, {"GPU_CUDA_ARCH": "90"}):
            result = get_gpu_cuda_arch()
            assert result == "90"


class TestNcclSettings:
    """Tests for NCCL test settings."""

    def test_get_nccl_image_default(self) -> None:
        """Test default NCCL image."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_image()
            assert "hpc-benchmarks" in result

    def test_get_nccl_timeout_default(self) -> None:
        """Test default NCCL timeout."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_timeout()
            assert result == 600

    def test_get_nccl_gpu_count_default(self) -> None:
        """Test default NCCL GPU count is None."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_gpu_count()
            assert result is None

    def test_get_nccl_gpu_count_custom(self) -> None:
        """Test custom NCCL GPU count."""
        with patch.dict(os.environ, {"NCCL_GPU_COUNT": "8"}):
            result = get_nccl_gpu_count()
            assert result == 8

    def test_get_nccl_gpu_count_zero(self) -> None:
        """Test NCCL GPU count explicitly set to zero returns 0, not None."""
        with patch.dict(os.environ, {"NCCL_GPU_COUNT": "0"}):
            result = get_nccl_gpu_count()
            assert result == 0

    def test_get_nccl_min_bus_bw_gbps_default(self) -> None:
        """Test default minimum bus bandwidth."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_min_bus_bw_gbps()
            assert result == 0.0

    def test_get_nccl_min_bus_bw_gbps_custom(self) -> None:
        """Test custom minimum bus bandwidth."""
        with patch.dict(os.environ, {"NCCL_MIN_BUS_BW_GBPS": "100.5"}):
            result = get_nccl_min_bus_bw_gbps()
            assert result == 100.5

    def test_get_nccl_results_hostpath_default(self) -> None:
        """Test default NCCL results host path."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_results_hostpath()
            assert result == "/data/nvstgt/results"


class TestNcclMultinodeSettings:
    """Tests for NCCL multi-node settings."""

    def test_get_nccl_hpc_image_default(self) -> None:
        """Test default HPC image."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_hpc_image()
            assert "hpc-benchmarks" in result

    def test_get_nccl_multinode_nodes_default(self) -> None:
        """Test default multi-node node count."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_multinode_nodes()
            assert result == 2

    def test_get_nccl_multinode_gpus_per_node_default(self) -> None:
        """Test default GPUs per node."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_multinode_gpus_per_node()
            assert result == 8

    def test_get_nccl_multinode_timeout_default(self) -> None:
        """Test default multi-node timeout."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nccl_multinode_timeout()
            assert result == 900


class TestNimHelmSettings:
    """Tests for NIM Helm workload settings."""

    def test_get_nim_helm_model_default(self) -> None:
        """Test default NIM model."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nim_helm_model()
            assert "llama" in result.lower()

    def test_get_nim_helm_model_tag_default(self) -> None:
        """Test default NIM model tag."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nim_helm_model_tag()
            assert result == "latest"

    def test_get_nim_helm_chart_version_default(self) -> None:
        """Test default Helm chart version."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nim_helm_chart_version()
            assert result == "1.15.1"

    def test_get_nim_helm_timeout_default(self) -> None:
        """Test default NIM Helm timeout."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nim_helm_timeout()
            assert result == 1800

    def test_get_nim_helm_gpu_count_default(self) -> None:
        """Test default NIM GPU count."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nim_helm_gpu_count()
            assert result == 1

    def test_get_nim_genai_perf_requests_default(self) -> None:
        """Test default GenAI-Perf requests."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nim_genai_perf_requests()
            assert result == 100

    def test_get_nim_genai_perf_concurrency_default(self) -> None:
        """Test default GenAI-Perf concurrency."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_nim_genai_perf_concurrency()
            assert result == 1
