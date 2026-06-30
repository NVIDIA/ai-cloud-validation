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

import json
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


def _write_provider_config(root: Path, provider: str, name: str, suite: str, platform: str) -> Path:
    """Write a minimal provider config importing one suite."""
    config_path = root / "providers" / provider / "config" / name
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""\
import:
  - ../../../suites/{suite}
commands:
  {platform}:
    phases: [test]
    steps: []
tests:
  platform: {platform}
""",
        encoding="utf-8",
    )
    return config_path


def _write_suite(root: Path, name: str, labels: list[str], check_name: str) -> None:
    """Write a provider-neutral suite with one check."""
    labels_yaml = ", ".join(f'"{label}"' for label in labels)
    suite_path = root / "suites" / name
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(
        f"""\
tests:
  validations:
    sample:
      checks:
        {check_name}:
          test_id: "N/A"
          labels: [{labels_yaml}]
""",
        encoding="utf-8",
    )


class _FakeOrchestrator:
    """Capture orchestrator options passed by the CLI for assertion in tests."""

    captured: ClassVar[dict[str, Any]] = {}
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, config: Any, **kwargs: Any) -> None:
        """Store constructor inputs so tests can assert CLI wiring."""
        self.config = config
        self.kwargs = kwargs

    def run(self, **kwargs: Any) -> OrchestratorResult:
        """Record a synthetic orchestration call and return a successful result."""
        type(self).captured.update(kwargs)
        type(self).calls.append(
            {
                "platform": self.config.tests.platform,
                "working_dir": self.kwargs.get("working_dir"),
                "run_kwargs": kwargs,
            }
        )
        return OrchestratorResult(
            success=True,
            phases=[PhaseResult(phase=Phase.TEST, success=True, message="ok")],
        )


def test_test_run_forwards_label_filters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`isvctl test run -l/--label` passes requested labels to the orchestrator."""
    config = _write_config(tmp_path)
    _FakeOrchestrator.captured = {}
    _FakeOrchestrator.calls = []
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
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--no-upload", "-l", "12345"])

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.captured["include_labels"] == ["12345"]


def test_label_without_provider_or_config_reports_both_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--label` with neither `--provider` nor `-f` names both ways to supply checks."""
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--label", "iam", "--no-upload"])

    assert result.exit_code == 1, result.output
    assert "--provider" in result.output
    assert "--config/-f" in result.output
    assert _FakeOrchestrator.calls == []


def test_provider_discovery_unknown_provider_lists_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unknown --provider reports it as unknown and lists the discoverable providers."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml", "network")
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "gcp", "--label", "network", "--no-upload"])

    assert result.exit_code == 1, result.output
    assert "Unknown provider 'gcp'" in result.output
    assert "aws" in result.output
    assert _FakeOrchestrator.calls == []


def test_provider_discovery_no_label_match_lists_available_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid provider with no label match reports the labels that provider does expose."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml", "network")
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "aws", "--label", "nope", "--no-upload"])

    assert result.exit_code == 1, result.output
    assert "Available labels for 'aws'" in result.output
    assert "network" in result.output
    assert _FakeOrchestrator.calls == []


def test_provider_label_discovery_dispatches_each_matching_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--provider --label` runs each matching provider config as its own lifecycle."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_suite(configs_root, "observability.yaml", ["network", "observability"], "VpcFlowLogsCheck")
    _write_suite(configs_root, "iam.yaml", ["iam"], "IamCheck")
    network_config = _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml", "network")
    observability_config = _write_provider_config(
        configs_root,
        "aws",
        "observability.yaml",
        "observability.yaml",
        "observability",
    )
    _write_provider_config(configs_root, "aws", "iam.yaml", "iam.yaml", "iam")
    _FakeOrchestrator.captured = {}
    _FakeOrchestrator.calls = []
    monkeypatch.setenv("ISVTEST_INCLUDE_UNRELEASED", "1")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "aws", "--label", "network", "--no-upload"])

    assert result.exit_code == 0, result.output
    assert [call["platform"] for call in _FakeOrchestrator.calls] == ["network", "observability"]
    assert [call["working_dir"] for call in _FakeOrchestrator.calls] == [
        network_config.parent,
        observability_config.parent,
    ]
    assert [call["run_kwargs"]["include_labels"] for call in _FakeOrchestrator.calls] == [["network"], ["network"]]


def test_provider_label_discovery_dry_run_prints_plan_without_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Discovery dry-run prints selected configs and checks without invoking the orchestrator."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_suite(configs_root, "observability.yaml", ["network", "observability"], "VpcFlowLogsCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml", "network")
    _write_provider_config(configs_root, "aws", "observability.yaml", "observability.yaml", "observability")
    _FakeOrchestrator.calls = []
    monkeypatch.setenv("ISVTEST_INCLUDE_UNRELEASED", "1")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "aws", "--label", "network", "--dry-run", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.calls == []
    plan = json.loads(result.output)
    assert plan["provider"] == "aws"
    assert plan["labels"] == ["network"]
    assert [Path(item["config"]).name for item in plan["configs"]] == ["network.yaml", "observability.yaml"]
    assert [item["matched_checks"][0]["name"] for item in plan["configs"]] == ["NetworkCheck", "VpcFlowLogsCheck"]
