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

"""ISV workload validations.

This module contains longer-running workload tests that deploy real workloads
to validate GPU functionality and performance.
"""

from isvtest.workloads.k8s_nccl import K8sNcclWorkload
from isvtest.workloads.k8s_nim import K8sNimInferenceWorkload
from isvtest.workloads.k8s_nim_helm import K8sNimHelmWorkload
from isvtest.workloads.k8s_stress import K8sGpuStressWorkload
from isvtest.workloads.slurm_gpu_stress import SlurmGpuStressWorkload
from isvtest.workloads.slurm_nccl_multinode import SlurmNcclMultiNodeWorkload
from isvtest.workloads.slurm_sbatch import SlurmSbatchWorkload

__all__ = [
    "K8sGpuStressWorkload",
    "K8sNcclWorkload",
    "K8sNimHelmWorkload",
    "K8sNimInferenceWorkload",
    "SlurmGpuStressWorkload",
    "SlurmNcclMultiNodeWorkload",
    "SlurmSbatchWorkload",
]
