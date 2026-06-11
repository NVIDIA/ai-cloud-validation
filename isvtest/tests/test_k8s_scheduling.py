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

"""Tests for k8s scheduling/capacity validations."""

from __future__ import annotations

import json
from unittest.mock import patch

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_scheduling import K8sGpuCapacityCheck


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def test_gpu_capacity_uses_node_json() -> None:
    """Verify GPU capacity is summed from node JSON capacity fields."""
    check = K8sGpuCapacityCheck(config={"expected_total": 4, "expected_per_node": 2})
    payload = json.dumps(
        {
            "items": [
                {"metadata": {"name": "gpu-1"}, "status": {"capacity": {"nvidia.com/gpu": "2"}}},
                {"metadata": {"name": "gpu-2"}, "status": {"capacity": {"nvidia.com/gpu": "2"}}},
            ]
        }
    )

    with (
        patch("isvtest.validations.k8s_scheduling.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok(payload)) as mock_run,
    ):
        check.run()

    assert check.passed
    assert mock_run.call_args[0][0] == "kubectl get nodes -o json"
