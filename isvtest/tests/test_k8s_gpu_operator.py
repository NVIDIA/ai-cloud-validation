# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for the GPU Operator pod-status validation."""

from __future__ import annotations

import json
from unittest.mock import patch

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_gpu_operator import K8sGpuOperatorPodsCheck


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def test_gpu_operator_pods_use_json_phase() -> None:
    """Verify GPU Operator pod status is parsed from JSON."""
    check = K8sGpuOperatorPodsCheck(config={"namespace": "gpu-operator"})
    payload = json.dumps({"items": [{"metadata": {"name": "gpu-operator-1"}, "status": {"phase": "Running"}}]})

    with (
        patch("isvtest.validations.k8s_gpu_operator.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok(payload)) as mock_run,
    ):
        check.run()

    assert check.passed
    assert mock_run.call_args[0][0] == "kubectl get pods -n gpu-operator -o json"
