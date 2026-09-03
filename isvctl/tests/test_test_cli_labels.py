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

from .conftest import ANSI_ESCAPE

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
  capability: kubernetes
  validations: {}
""",
        encoding="utf-8",
    )
    return config


def test_validation_result_detail_compacts_successful_subtests() -> None:
    """Successful validations with probes render an aggregate instead of their long message."""
    detail = test_cli._validation_result_detail(
        {
            "passed": True,
            "skipped": False,
            "state": "passed",
            "message": "long; member; output",
            "subtest_summary": {"total": 6, "passed": 6, "failed": 0, "skipped": 0},
        }
    )

    assert detail == "6 subtests passed"


def test_validation_result_detail_preserves_failures() -> None:
    """Failed validations keep their actionable message even when they report probes."""
    detail = test_cli._validation_result_detail(
        {
            "passed": False,
            "skipped": False,
            "state": "failed",
            "message": "RdmaCheck: worker-a -> worker-b timed out",
            "subtest_summary": {"total": 6, "passed": 5, "failed": 1, "skipped": 0},
        }
    )

    assert detail == "RdmaCheck: worker-a -> worker-b timed out"


def test_validation_result_detail_counts_successful_runs_with_skips() -> None:
    """An allowed skipped probe remains visible in the concise success summary."""
    detail = test_cli._validation_result_detail(
        {
            "passed": True,
            "skipped": False,
            "state": "passed",
            "message": "long output",
            "subtest_summary": {"total": 6, "passed": 5, "failed": 0, "skipped": 1},
        }
    )

    assert detail == "6 subtests: 5 passed, 0 failed, 1 skipped"


def test_validation_result_detail_derives_total_for_legacy_summaries() -> None:
    """Older result payloads remain concise when they omit the explicit total."""
    detail = test_cli._validation_result_detail(
        {
            "passed": True,
            "skipped": False,
            "state": "passed",
            "message": "long output",
            "subtest_summary": {"passed": 5, "failed": 0, "skipped": 1},
        }
    )

    assert detail == "6 subtests: 5 passed, 0 failed, 1 skipped"


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
                "platform": self.config.tests.capability,
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


def test_orchestration_summary_hides_filtered_validations_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The default summary omits phases containing only selection-filtered checks."""
    config = _write_config(tmp_path)

    class FilteredOrchestrator(_FakeOrchestrator):
        """Return one resolution-only phase for a filtered validation."""

        def run(self, **kwargs: Any) -> OrchestratorResult:
            """Return the synthetic filtered result."""
            return OrchestratorResult(
                success=True,
                phases=[
                    PhaseResult(
                        phase=Phase.TEST,
                        success=True,
                        message="test phase validations resolved without execution",
                        details={
                            "validations": [
                                {
                                    "name": "InfiniBandCheck",
                                    "skipped": True,
                                    "state": "skipped",
                                    "skip_reason": "test_excluded",
                                    "message": "does not match selected label ethernet",
                                }
                            ]
                        },
                    )
                ],
            )

    monkeypatch.setattr(test_cli, "Orchestrator", FilteredOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--label", "ethernet", "--no-upload"])

    assert result.exit_code == 0, result.output
    assert "InfiniBandCheck" not in result.output
    assert "TEST                    : test phase validations resolved" not in result.output


def test_test_run_uploads_the_complete_catalog_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The automatic result path forwards the same complete catalog it saves."""
    config = _write_config(tmp_path)
    output_dir = tmp_path / "_output"
    output_dir.mkdir()
    document = {
        "schemaVersion": 2,
        "isvTestVersion": "1.2.3",
        "platforms": ["kubernetes", "vm"],
        "suites": ["storage"],
        "entries": [{"name": "TestA"}],
    }
    captured: dict[str, Any] = {}

    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(test_cli, "get_output_dir", lambda: output_dir)
    monkeypatch.setattr(test_cli, "check_upload_credentials", lambda: (True, "client-id", "client-secret"))
    monkeypatch.setattr(
        test_cli,
        "get_environment_config",
        lambda: ("https://api.example.com", "https://ssa.example.com"),
    )
    monkeypatch.setattr(test_cli, "create_test_run", lambda **_kwargs: "run-123")
    monkeypatch.setattr(test_cli, "build_catalog", lambda: document["entries"])
    monkeypatch.setattr(test_cli, "get_catalog_version", lambda: document["isvTestVersion"])
    monkeypatch.setattr(test_cli, "catalog_document", lambda _entries, _version: document)

    def capture_update(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(test_cli, "update_test_run", capture_update)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--lab-id", "7"])

    assert result.exit_code == 0, result.output
    assert captured["catalog_document"] == document
    assert json.loads((output_dir / "test_catalog.json").read_text()) == document


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
    assert [call["platform"] for call in _FakeOrchestrator.calls] == [None, None]
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


def test_provider_without_suite_or_label_mentions_both_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--provider` alone tells the user to pick `--suite` or `--label`."""
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "aws", "--dry-run", "--no-upload"])

    assert result.exit_code == 1, result.output
    assert "--suite NAME" in result.output
    assert "--label/-l" in result.output
    assert _FakeOrchestrator.calls == []


def test_suite_without_provider_runs_the_canonical_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Providerless --suite selects the canonical suite file directly."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "storage.yaml", ["storage"], "K8sCsiStorageTypesCheck")
    (configs_root / "suites" / "k8s.yaml").write_text(
        "tests:\n  capability: kubernetes\n  validations: {}\n",
        encoding="utf-8",
    )
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        [
            "run",
            "--suite",
            "storage",
            "--capability",
            "kubernetes",
            "--phase",
            "test",
            "--no-upload",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Selected canonical 'storage' suite." in result.output
    assert len(_FakeOrchestrator.calls) == 1
    assert _FakeOrchestrator.calls[0]["working_dir"] == configs_root / "suites"
    assert _FakeOrchestrator.calls[0]["run_kwargs"]["phases"] == [Phase.TEST]
    assert _FakeOrchestrator.calls[0]["run_kwargs"]["capability"] == "kubernetes"


def test_plain_suite_without_capability_defaults_to_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A declared plain suite runs core checks unless a capability is selected."""
    configs_root = tmp_path / "configs"
    suite_path = configs_root / "suites" / "storage.yaml"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text(
        """\
tests:
  validations:
    core:
      checks:
        CoreCheck:
          test_id: "N/A"
          labels: ["storage"]
          requires: []
    vm:
      checks:
        VmCheck:
          test_id: "N/A"
          labels: ["storage"]
          requires: [vm]
""",
        encoding="utf-8",
    )
    (configs_root / "suites" / "vm.yaml").write_text(
        "tests:\n  capability: vm\n  validations: {}\n",
        encoding="utf-8",
    )
    _write_provider_config(configs_root, "aws", "storage.yaml", "storage.yaml", "storage")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)

    core_result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "aws", "--suite", "storage", "--dry-run", "--no-upload"],
    )
    vm_result = runner.invoke(
        test_cli.app,
        [
            "run",
            "--provider",
            "aws",
            "--suite",
            "storage",
            "--capability",
            "vm",
            "--dry-run",
            "--no-upload",
        ],
    )

    assert core_result.exit_code == 0, core_result.output
    assert "Capability: core" in core_result.stdout
    assert "[RUN]  CoreCheck" in core_result.stdout
    assert "[SKIP] VmCheck" in core_result.stdout
    assert vm_result.exit_code == 0, vm_result.output
    assert "Capability: vm" in vm_result.stdout
    assert "[RUN]  CoreCheck" in vm_result.stdout
    assert "[RUN]  VmCheck" in vm_result.stdout


def test_every_entry_path_resolves_the_same_capability_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One config runs the same checks whether reached via --suite or -f.

    The context models what an ISV declared, which cannot depend on how the
    config was named on the command line.
    """
    configs_root = tmp_path / "configs"
    suite_path = configs_root / "suites" / "storage.yaml"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text(
        """\
tests:
  validations:
    core:
      checks:
        CoreCheck:
          test_id: "N/A"
          labels: ["storage"]
          requires: []
    vm:
      checks:
        VmCheck:
          test_id: "N/A"
          labels: ["storage"]
          requires: [vm]
""",
        encoding="utf-8",
    )
    (configs_root / "suites" / "vm.yaml").write_text(
        "tests:\n  capability: vm\n  validations: {}\n",
        encoding="utf-8",
    )
    provider_config = _write_provider_config(configs_root, "aws", "storage.yaml", "storage.yaml", "storage")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)

    via_suite = runner.invoke(
        test_cli.app,
        ["run", "--provider", "aws", "--suite", "storage", "--dry-run", "--no-upload"],
    )
    via_file = runner.invoke(
        test_cli.app,
        ["run", "-f", str(provider_config), "--dry-run", "--no-upload"],
    )

    assert via_suite.exit_code == 0, via_suite.output
    assert via_file.exit_code == 0, via_file.output
    for output in (via_suite.stdout, via_file.stdout):
        assert "Capability: core" in output
        assert "[RUN]  CoreCheck" in output
        assert "[SKIP] VmCheck" in output


def test_platform_suite_uses_declared_capability_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Platform suites filter imported requirement metadata using their declaration."""
    configs_root = tmp_path / "configs"
    suites = configs_root / "suites"
    suites.mkdir(parents=True)
    (suites / "vm.yaml").write_text(
        """\
tests:
  capability: vm
  validations:
    vm:
      checks:
        PlatformCheck:
          test_id: "N/A"
          labels: ["vm"]
        K8sImportedCheck:
          test_id: "N/A"
          labels: ["kubernetes"]
          requires: [kubernetes]
""",
        encoding="utf-8",
    )
    _write_provider_config(configs_root, "aws", "vm.yaml", "vm.yaml", "vm")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "aws", "--suite", "vm", "--dry-run", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert "Capability: vm" in result.stdout
    assert "[RUN]  PlatformCheck" in result.stdout
    assert "[SKIP] K8sImportedCheck: requires kubernetes (context: vm)" in result.stdout


def test_platform_suite_rejects_explicit_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A platform suite cannot be relabeled with an explicit capability."""
    configs_root = tmp_path / "configs"
    suites = configs_root / "suites"
    suites.mkdir(parents=True)
    (suites / "vm.yaml").write_text(
        "tests:\n  capability: vm\n  validations: {}\n",
        encoding="utf-8",
    )
    (suites / "k8s.yaml").write_text(
        "tests:\n  capability: kubernetes\n  validations: {}\n",
        encoding="utf-8",
    )
    _write_provider_config(configs_root, "aws", "vm.yaml", "vm.yaml", "vm")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)

    result = runner.invoke(
        test_cli.app,
        [
            "run",
            "--provider",
            "aws",
            "--suite",
            "vm",
            "--capability",
            "kubernetes",
            "--dry-run",
            "--no-upload",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "--capability cannot be used with platform suite 'vm'" in result.output


def test_capability_with_no_matching_check_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A capability no check in the suite requires is a likely mistake."""
    configs_root = tmp_path / "configs"
    suite_path = configs_root / "suites" / "iam.yaml"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text(
        """\
tests:
  validations:
    iam:
      checks:
        CoreCheck:
          test_id: "N/A"
          labels: ["iam"]
          requires: []
""",
        encoding="utf-8",
    )
    (configs_root / "suites" / "kubernetes.yaml").write_text(
        "tests:\n  capability: kubernetes\n  validations: {}\n",
        encoding="utf-8",
    )
    _write_provider_config(configs_root, "aws", "iam.yaml", "iam.yaml", "iam")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "aws", "--suite", "iam", "--capability", "kubernetes", "--dry-run", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    # print_warning goes to stderr; the dry-run body to stdout.
    assert "No check in 'iam' requires kubernetes" in result.stderr
    assert "[RUN]  CoreCheck" in result.stdout


def test_suite_and_label_filters_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A suite selects one lifecycle while labels narrow its checks."""
    configs_root = tmp_path / "configs"
    suite_path = configs_root / "suites" / "storage.yaml"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text(
        """\
tests:
  validations:
    storage:
      checks:
        FastCheck:
          test_id: "N/A"
          labels: ["storage"]
        SlowCheck:
          test_id: "N/A"
          labels: ["storage", "slow"]
        DestructiveSlowCheck:
          test_id: "N/A"
          labels: ["storage", "slow", "destructive"]
""",
        encoding="utf-8",
    )
    _write_provider_config(configs_root, "aws", "storage.yaml", "storage.yaml", "storage")
    _FakeOrchestrator.captured = {}
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    command = [
        "run",
        "--provider",
        "aws",
        "--suite",
        "storage",
        "--label",
        "slow",
        "--exclude-label",
        "destructive",
        "--no-upload",
    ]
    run_result = runner.invoke(test_cli.app, command)
    dry_run_result = runner.invoke(test_cli.app, [*command, "--dry-run"])

    assert run_result.exit_code == 0, run_result.output
    assert _FakeOrchestrator.captured["include_labels"] == ["slow"]
    assert _FakeOrchestrator.captured["exclude_labels"] == ["destructive"]
    assert dry_run_result.exit_code == 0, dry_run_result.output
    assert "Labels: slow (all required)" in dry_run_result.stdout
    assert "Excluded labels: destructive" in dry_run_result.stdout
    assert "[SKIP] FastCheck: does not match all selected labels: slow" in dry_run_result.stdout
    assert "[RUN]  SlowCheck" in dry_run_result.stdout
    assert "[SKIP] DestructiveSlowCheck: excluded by label: destructive" in dry_run_result.stdout


def test_unknown_option_before_separator_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stale flags like `--platform` fail before they can be forwarded to pytest."""
    config = _write_config(tmp_path)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "-f", str(config), "--platform", "k8s", "--no-upload", "--dry-run"],
    )

    # Typer forces rich styling under GITHUB_ACTIONS, which splices escape
    # codes into the middle of the reported option name.
    output = ANSI_ESCAPE.sub("", result.output)

    assert result.exit_code != 0, output
    assert "No such option" in output or "no such option" in output.lower()
    assert "--platform" in output
    assert _FakeOrchestrator.calls == []


def test_pytest_args_after_separator_are_forwarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Args after `--` still reach the orchestrator as pytest extras."""
    config = _write_config(tmp_path)
    _FakeOrchestrator.captured = {}
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "-f", str(config), "--no-upload", "--", "-k", "K8sNodeCountCheck"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.captured["extra_pytest_args"] == ["-k", "K8sNodeCountCheck"]


@pytest.mark.parametrize(("color", "styled"), [("yes", True), ("no", False)])
def test_color_choice_applies_to_the_results_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    color: str,
    styled: bool,
) -> None:
    """--color governs this command's output, not only the pytest args it builds.

    Under `deploy` the remote stdout is a pipe, so click's auto-detection would
    hand back an unstyled summary alongside colored pytest output.
    """
    config = _write_config(tmp_path)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "-f", str(config), "--no-upload", f"--color={color}"],
        color=True,
    )

    assert result.exit_code == 0, result.output
    assert bool(ANSI_ESCAPE.search(result.output)) is styled
    assert _FakeOrchestrator.captured["extra_pytest_args"] == [f"--color={color}"]
