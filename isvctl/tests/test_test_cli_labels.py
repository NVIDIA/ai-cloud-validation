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

"""Tests for isvctl test CLI label filtering."""

from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

import isvctl.cli.test as test_cli
from isvctl.orchestrator.loop import OrchestratorResult, Phase, PhaseResult

runner = CliRunner()


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal isvctl test config and return its path."""
    config = tmp_path / "config.yaml"
    config.write_text(
        """
commands:
  kubernetes:
    phases: [test]
    steps:
      - name: test_step
        command: echo
        args: ['{"success": true}']
        phase: test
tests:
  platform: kubernetes
  validations: {}
""",
        encoding="utf-8",
    )
    return config


class _FakeOrchestrator:
    """Capture orchestrator options passed by the CLI for assertion in tests."""

    captured: ClassVar[dict[str, Any]] = {}

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def run(self, **kwargs: Any) -> OrchestratorResult:
        type(self).captured.update(kwargs)
        return OrchestratorResult(
            success=True,
            phases=[PhaseResult(phase=Phase.TEST, success=True, message="ok")],
        )


def test_test_run_forwards_label_filters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`isvctl test run -l/--label` passes requested labels to the orchestrator."""
    config = _write_config(tmp_path)
    _FakeOrchestrator.captured = {}
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--no-upload", "-l", "gpu", "--label", "slow"])

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.captured["include_labels"] == ["gpu", "slow"]


def test_short_l_flag_binds_to_label_not_lab_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`-l` is the short flag for `--label`, not the legacy `--lab-id`.

    Pre-PR, `-l 12345` mapped to `--lab-id 12345` (an int). This test pins the
    rebinding so a future refactor can't silently restore the old mapping: an
    all-digit value passed via `-l` must land in ``include_labels`` as a string,
    and the orchestrator must run with the request (proving Typer did not reject
    a non-int `--lab-id`).
    """
    config = _write_config(tmp_path)
    _FakeOrchestrator.captured = {}
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--no-upload", "-l", "12345"])

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.captured["include_labels"] == ["12345"]
