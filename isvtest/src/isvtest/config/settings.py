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

"""Application settings."""

import os
from dataclasses import dataclass


@dataclass
class Settings:
    """Application configuration settings.

    Environment Variables:
        VALIDATION_TIMEOUT: Timeout for validation operations in seconds (default: 300)
        LOG_LEVEL: Logging level (default: INFO)
        REFRAME_PATH: Path to ReFrame validation scripts (default: workloads)
        SLURM_PATH: Path to Slurm validation scripts (default: validations)
        K8S_PATH: Path to Kubernetes validation scripts (default: validations)
        K8S_PROVIDER: Kubernetes provider ("kubectl", "microk8s", "minikube", or "k3s", default: kubectl)
        K8S_GPU_OPERATOR_NAMESPACE: GPU operator namespace (default: auto-detect based on provider)
        K8S_NAMESPACE: Kubernetes namespace for test pods (default: default)
        SKIP_K8S_TESTS: Set to "true" to skip Kubernetes tests (default: false)

        GPU Stress Test Configuration:
        GPU_STRESS_IMAGE: Container image for GPU stress test (default: nvcr.io/nvidia/pytorch:25.04-py3)
        GPU_STRESS_RUNTIME: GPU stress test runtime in seconds (default: 300)
        GPU_STRESS_TIMEOUT: Total timeout for test pod (runtime + overhead, default: 420)
        GPU_STRESS_GPU_COUNT: Number of GPUs to request per pod (default: auto-detect all)
        GPU_MEMORY_GB: GPU memory size in GB for stress test (default: 32)
        GPU_CUDA_ARCH: CUDA compute capability for CuPy on ARM64 (e.g., "80" for A100, "90" for H100, default: auto-detect)

        NCCL Allreduce Test Configuration:
        NCCL_IMAGE: Container image for NCCL test (default: nvcr.io/nvidia/hpc-benchmarks:25.04)
        NCCL_TIMEOUT: Total timeout for NCCL test job (default: 600 = 10 minutes)
        NCCL_GPU_COUNT: Number of GPUs to request per job (default: auto-detect all)
        NCCL_MIN_BUS_BW_GBPS: Minimum expected bus bandwidth in GB/s (default: 0 = no check)
        NCCL_RESULTS_HOSTPATH: Host path for NCCL results volume (default: /data/nvstgt/results)

        NCCL Multi-Node Test Configuration:
        NCCL_HPC_IMAGE: HPC benchmarks container for multi-node NCCL (default: nvcr.io/nvidia/hpc-benchmarks:25.04)
        NCCL_MULTINODE_NODES: Number of nodes for multi-node test (default: 2)
        NCCL_MULTINODE_GPUS_PER_NODE: GPUs per node for multi-node test (default: 8)
        NCCL_MULTINODE_TIMEOUT: Timeout for multi-node NCCL test (default: 900 = 15 minutes)

        NIM Helm Workload Configuration:
        NIM_HELM_MODEL: NIM model to deploy (default: meta/llama-3.2-3b-instruct)
        NIM_HELM_MODEL_TAG: NIM model tag (default: latest)
        NIM_HELM_CHART_VERSION: Helm chart version (default: 1.15.1)
        NIM_HELM_TIMEOUT: Total timeout for NIM workload (default: 1800 = 30 minutes)
        NIM_HELM_GPU_COUNT: Number of GPUs for NIM (default: 1)
        NIM_GENAI_PERF_REQUESTS: Number of GenAI-Perf test requests (default: 100)
        NIM_GENAI_PERF_CONCURRENCY: GenAI-Perf concurrent requests (default: 1)

        Dual-Stack Node Validation Configuration (K8S22):
        K8S_REQUIRE_DUAL_STACK: Whether the cluster must be dual-stack
            ("true", "false", or "auto", default: "auto")

        NetworkPolicy Validation Configuration (K8S22):
        K8S_NETPOL_IMAGE: Container image for NetworkPolicy probe pods
            (default: registry.k8s.io/e2e-test-images/agnhost:2.47)
    """

    validation_timeout: int = int(os.getenv("VALIDATION_TIMEOUT", "300"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    reframe_path: str = os.getenv("REFRAME_PATH", "workloads")
    slurm_path: str = os.getenv("SLURM_PATH", "validations")
    k8s_path: str = os.getenv("K8S_PATH", "validations")
    k8s_provider: str = os.getenv("K8S_PROVIDER", "kubectl")


def get_k8s_gpu_operator_namespace() -> str:
    """Get GPU operator namespace based on provider.

    Returns:
        Namespace name for GPU operator (microk8s uses gpu-operator-resources,
        standard kubectl uses gpu-operator)
    """
    # Allow explicit override
    if override := os.getenv("K8S_GPU_OPERATOR_NAMESPACE"):
        return override

    # Auto-detect based on provider
    provider = os.getenv("K8S_PROVIDER", "kubectl")
    if provider == "microk8s":
        return "gpu-operator-resources"
    return "nvidia-gpu-operator"


def get_k8s_namespace() -> str:
    """Get Kubernetes namespace for test pods.

    Returns:
        Namespace for test pods (default: default)
    """
    return os.getenv("K8S_NAMESPACE", "default")


def get_gpu_stress_image() -> str:
    """Get container image for GPU stress test.

    Returns:
        Container image URL
    """
    return os.getenv("GPU_STRESS_IMAGE", "nvcr.io/nvidia/pytorch:25.04-py3")


def get_gpu_stress_runtime() -> int:
    """Get GPU stress test runtime in seconds.

    Returns:
        Runtime in seconds (default: 30)
    """
    return int(os.getenv("GPU_STRESS_RUNTIME", "30"))


def get_gpu_stress_timeout() -> int:
    """Get GPU stress test total timeout (runtime + overhead).

    Returns:
        Timeout in seconds (default: 420 = 7 minutes)
    """
    return int(os.getenv("GPU_STRESS_TIMEOUT", "420"))


def get_gpu_stress_gpu_count() -> int | None:
    """Get number of GPUs to request per pod.

    Returns:
        GPU count, or None for auto-detect
    """
    if count := os.getenv("GPU_STRESS_GPU_COUNT"):
        return int(count)
    return None


def get_gpu_memory_gb() -> int:
    """Get GPU memory size in GB for stress test.

    Returns:
        GPU memory in GB (default: 32)
    """
    return int(os.getenv("GPU_MEMORY_GB", "32"))


def get_gpu_cuda_arch() -> str | None:
    """Get CUDA compute capability for CuPy on ARM64.

    Returns:
        CUDA arch string (e.g., "80" for A100, "90" for H100), or None for auto-detect
    """
    return os.getenv("GPU_CUDA_ARCH")


def get_nccl_image() -> str:
    """Get container image for NCCL test.

    Returns:
        Container image URL
    """
    return os.getenv(
        "NCCL_IMAGE",
        "nvcr.io/nvidia/hpc-benchmarks:25.04",
    )


def get_nccl_timeout() -> int:
    """Get NCCL test total timeout.

    Returns:
        Timeout in seconds (default: 600 = 10 minutes)
    """
    return int(os.getenv("NCCL_TIMEOUT", "600"))


def get_nccl_gpu_count() -> int | None:
    """Get number of GPUs to request per NCCL job.

    Returns:
        GPU count, or None for auto-detect
    """
    if count := os.getenv("NCCL_GPU_COUNT"):
        return int(count)
    return None


def get_nccl_min_bus_bw_gbps() -> float:
    """Get minimum expected bus bandwidth in GB/s.

    Returns:
        Minimum bus bandwidth in GB/s (default: 0 = no check)
    """
    return float(os.getenv("NCCL_MIN_BUS_BW_GBPS", "0"))


def get_nccl_results_hostpath() -> str:
    """Get host path for NCCL results volume.

    Returns:
        Host path for results (default: /data/nvstgt/results)
    """
    return os.getenv("NCCL_RESULTS_HOSTPATH", "/data/nvstgt/results")


# NCCL Multi-Node Workload Settings


def get_nccl_hpc_image() -> str:
    """Get HPC benchmarks container image for multi-node NCCL test.

    Returns:
        Container image URL (default: nvcr.io/nvidia/hpc-benchmarks:25.04)
    """
    return os.getenv("NCCL_HPC_IMAGE", "nvcr.io/nvidia/hpc-benchmarks:25.04")


def get_nccl_multinode_nodes() -> int:
    """Get number of nodes for multi-node NCCL test.

    Returns:
        Number of nodes (default: 2)
    """
    return int(os.getenv("NCCL_MULTINODE_NODES", "2"))


def get_nccl_multinode_gpus_per_node() -> int:
    """Get GPUs per node for multi-node NCCL test.

    Returns:
        GPUs per node (default: 8)
    """
    return int(os.getenv("NCCL_MULTINODE_GPUS_PER_NODE", "8"))


def get_nccl_multinode_timeout() -> int:
    """Get timeout for multi-node NCCL test.

    Returns:
        Timeout in seconds (default: 900 = 15 minutes)
    """
    return int(os.getenv("NCCL_MULTINODE_TIMEOUT", "900"))


# NIM Helm Workload Settings


def get_nim_helm_model() -> str:
    """Get NIM model to deploy via Helm.

    Returns:
        Model name (default: meta/llama-3.2-3b-instruct)
    """
    return os.getenv("NIM_HELM_MODEL", "meta/llama-3.2-3b-instruct")


def get_nim_helm_model_tag() -> str:
    """Get NIM model tag.

    Returns:
        Model tag (default: latest)
    """
    return os.getenv("NIM_HELM_MODEL_TAG", "latest")


def get_nim_helm_chart_version() -> str:
    """Get NIM Helm chart version.

    Returns:
        Chart version (default: 1.15.1)
    """
    return os.getenv("NIM_HELM_CHART_VERSION", "1.15.1")


def get_nim_helm_timeout() -> int:
    """Get NIM Helm workload total timeout.

    Returns:
        Timeout in seconds (default: 1800 = 30 minutes)
    """
    return int(os.getenv("NIM_HELM_TIMEOUT", "1800"))


def get_nim_helm_gpu_count() -> int:
    """Get number of GPUs for NIM deployment.

    Returns:
        GPU count (default: 1)
    """
    return int(os.getenv("NIM_HELM_GPU_COUNT", "1"))


def get_nim_genai_perf_requests() -> int:
    """Get number of GenAI-Perf test requests.

    Returns:
        Number of requests (default: 100)
    """
    return int(os.getenv("NIM_GENAI_PERF_REQUESTS", "100"))


def get_nim_genai_perf_concurrency() -> int:
    """Get GenAI-Perf concurrent requests.

    Returns:
        Concurrency level (default: 1)
    """
    return int(os.getenv("NIM_GENAI_PERF_CONCURRENCY", "1"))


def get_k8s_require_dual_stack() -> str:
    """Get the dual-stack requirement for ``K8sDualStackNodeCheck``.

    Returns:
        One of ``"true"``, ``"false"``, or ``"auto"`` (default: ``"auto"``).
        The validation itself normalizes these to bool / ``"auto"``.
    """
    return os.getenv("K8S_REQUIRE_DUAL_STACK", "auto")


def get_k8s_csi_block_storage_class() -> str:
    """Get StorageClass name for CSI block storage validation.

    Returns:
        StorageClass name (default: empty string - subtest will skip).
    """
    return os.getenv("K8S_CSI_BLOCK_SC", "")


def get_k8s_csi_shared_fs_storage_class() -> str:
    """Get StorageClass name for CSI shared filesystem (RWX) validation.

    Returns:
        StorageClass name (default: empty string - subtest will skip).
    """
    return os.getenv("K8S_CSI_SHARED_FS_SC", "")


def get_k8s_csi_nfs_storage_class() -> str:
    """Get StorageClass name for CSI NFS validation.

    Returns:
        StorageClass name (default: empty string - subtest will skip).
    """
    return os.getenv("K8S_CSI_NFS_SC", "")


def get_k8s_network_policy_image() -> str:
    """Get container image for NetworkPolicy probe pods.

    The image must provide the ``agnhost`` binary (supports both ``connect``
    and ``netexec`` subcommands).

    Returns:
        Container image URL (default: registry.k8s.io/e2e-test-images/agnhost:2.47)
    """
    return os.getenv("K8S_NETPOL_IMAGE", "registry.k8s.io/e2e-test-images/agnhost:2.47")


settings = Settings()
