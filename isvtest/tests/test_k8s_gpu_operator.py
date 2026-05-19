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
