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

"""Tests that NCCL and CUDA manifests take their image from settings."""

import os
from pathlib import Path
from unittest.mock import patch

from isvtest.config.settings import (
    CUDA_IMAGE_PLACEHOLDER,
    DEFAULT_CUDA_IMAGE,
    DEFAULT_NCCL_IMAGE,
    NCCL_IMAGE_PLACEHOLDER,
    get_nccl_image,
)
from isvtest.workloads.k8s_nccl import render_k8s_nccl_job
from isvtest.workloads.k8s_nccl_multinode import K8sNcclMultiNodeWorkload

_MANIFESTS = Path(__file__).resolve().parents[1] / "src" / "isvtest" / "workloads" / "manifests" / "k8s"
_OVERRIDE = "example.invalid/nccl:override"


def test_single_node_job_uses_nccl_image_override() -> None:
    """NCCL_IMAGE is applied to the single-node Job, including the default tag."""
    manifest = (_MANIFESTS / "nccl_allreduce_job.yaml").read_text()
    assert NCCL_IMAGE_PLACEHOLDER in manifest
    assert "nvcr.io/nvidia/hpc-benchmarks:" not in manifest

    with patch.dict(os.environ, {}, clear=True):
        rendered = render_k8s_nccl_job(manifest, job_name="nccl-job", gpu_count=2, image=get_nccl_image())
    assert f"image: {DEFAULT_NCCL_IMAGE}" in rendered
    assert NCCL_IMAGE_PLACEHOLDER not in rendered

    overridden = render_k8s_nccl_job(manifest, job_name="nccl-job", gpu_count=2, image=_OVERRIDE)
    assert f"image: {_OVERRIDE}" in overridden
    assert DEFAULT_NCCL_IMAGE not in overridden


def test_multinode_mpijob_replaces_both_image_slots() -> None:
    """A tag bump cannot leave NCCL_HPC_IMAGE matching a stale literal."""
    manifest = (_MANIFESTS / "nccl_allreduce_mpijob.yaml").read_text()
    assert manifest.count(NCCL_IMAGE_PLACEHOLDER) == 2

    workload = K8sNcclMultiNodeWorkload()
    rendered = workload._patch_manifest(
        manifest,
        job_name="nccl-mn",
        node_count=2,
        gpus_per_node=4,
        total_gpus=8,
        image=_OVERRIDE,
    )
    assert rendered.count(f"image: {_OVERRIDE}") == 2
    assert NCCL_IMAGE_PLACEHOLDER not in rendered
    assert DEFAULT_NCCL_IMAGE not in rendered


def test_nim_sidecar_uses_shared_cuda_image() -> None:
    """The NIM probe sidecar uses the same CUDA base image as the other probes."""
    manifest = (_MANIFESTS / "nim_llama_3b_inference_job.yaml").read_text()
    assert f"image: {CUDA_IMAGE_PLACEHOLDER}" in manifest
    assert "nvcr.io/nvidia/cuda:" not in manifest
    assert DEFAULT_CUDA_IMAGE not in manifest
