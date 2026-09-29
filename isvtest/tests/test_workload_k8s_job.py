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

"""Tests for ``BaseWorkloadCheck.run_k8s_job``."""

import subprocess
from unittest.mock import patch

from isvtest.core.runners import CommandResult
from isvtest.core.workload import BaseWorkloadCheck


class _JobWorkload(BaseWorkloadCheck):
    def run(self) -> None:
        pass


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr="", duration=0.0)


def test_timeout_reports_pod_logs_before_the_job_is_deleted() -> None:
    """A job whose pods keep failing times out; the pod logs are the only clue why."""
    workload = _JobWorkload()
    commands: list[str] = []

    def fake_run_command(cmd: str, **_: object) -> CommandResult:
        commands.append(cmd)
        if " logs " in cmd:
            return _ok("[pod/nccl-abc/nccl] line 1\n[pod/nccl-abc/nccl] all_reduce_perf_mpi: not found\n")
        return _ok()

    applied = subprocess.CompletedProcess(args=[], returncode=0, stdout="job created", stderr="")
    with (
        patch("isvtest.core.workload.get_kubectl_command", return_value=["kubectl"]),
        patch("isvtest.core.workload.get_kubectl_base_shell", return_value="kubectl"),
        patch("isvtest.core.workload.subprocess.run", return_value=applied),
        patch.object(workload, "run_command", side_effect=fake_run_command),
    ):
        result = workload.run_k8s_job(job_name="nccl", namespace="default", yaml_content="", timeout=0)

    assert result.exit_code == -1
    assert "Job timed out" in result.stderr
    assert "all_reduce_perf_mpi: not found" in result.stderr
    logs_index = next(i for i, c in enumerate(commands) if " logs " in c)
    delete_index = next(i for i, c in enumerate(commands) if " delete job " in c)
    assert "-l job-name=nccl" in commands[logs_index]
    assert logs_index < delete_index
