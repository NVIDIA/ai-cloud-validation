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

from .conftest import write_axis_provider_config, write_axis_suite

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


def _build_platform_provider(configs_root: Path) -> None:
    """Build an ``acme`` provider with vm/bare_metal platforms + iam/network modules.

    Both modules carry session lifecycles for every platform, so they join
    each real column's session plan.
    """
    write_axis_suite(configs_root, "vm.yaml", "vm", "platform")
    write_axis_suite(configs_root, "bare_metal.yaml", "bare_metal", "platform")
    write_axis_suite(configs_root, "iam.yaml", "iam", "module")
    write_axis_suite(configs_root, "network.yaml", "network", "module")
    write_axis_provider_config(configs_root, "acme", "vm.yaml", "vm.yaml", run_platform="vm")
    write_axis_provider_config(configs_root, "acme", "bare_metal.yaml", "bare_metal.yaml", run_platform="bare_metal")
    write_axis_provider_config(
        configs_root,
        "acme",
        "iam.yaml",
        "iam.yaml",
        run_platform="iam",
        session_keys=["iam@vm", "iam@bare_metal", "iam@kubernetes"],
    )
    write_axis_provider_config(
        configs_root,
        "acme",
        "network.yaml",
        "network.yaml",
        run_platform="network",
        session_keys=["network@vm", "network@bare_metal", "network@kubernetes"],
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
        """Record a synthetic orchestration call and return a successful result.

        Mirrors the real orchestrator's platform-session bracket: a provided
        ``pre_teardown_hook`` fires (with fake step outputs and success) after
        this run is recorded, so session module runs land in ``calls`` between
        the platform run and its teardown, exactly as in production.
        """
        type(self).captured.update(kwargs)
        type(self).calls.append(
            {
                "platform": self.config.tests.platform,
                "working_dir": self.kwargs.get("working_dir"),
                "run_kwargs": kwargs,
            }
        )
        hook = kwargs.get("pre_teardown_hook")
        if hook is not None:
            hook({"setup": {"success": True}}, True)
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
        ["run", "--provider", "aws", "--label", "network", "--dry-run", "--json", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.calls == []
    plan = json.loads(result.output)
    assert plan["provider"] == "aws"
    assert plan["labels"] == ["network"]
    assert [Path(item["config"]).name for item in plan["configs"]] == ["network.yaml", "observability.yaml"]
    assert [item["matched_checks"][0]["name"] for item in plan["configs"]] == ["NetworkCheck", "VpcFlowLogsCheck"]


def test_provider_label_discovery_dry_run_text_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without --json, the discovery dry-run prints a readable summary, not JSON."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml", "network")
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
    assert "Dry run: provider label discovery" in result.output
    assert "NetworkCheck" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_platform_dispatches_platform_then_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--platform vm` runs the vm config first, then each module under the column."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--platform", "vm", "--no-upload"])

    assert result.exit_code == 0, result.output
    assert [call["platform"] for call in _FakeOrchestrator.calls] == ["vm", "iam", "network"]
    # no auto-derived exclude labels; every run carries the column platform so
    # platforms: restrictions are applied at resolution time
    assert [call["run_kwargs"].get("exclude_labels") for call in _FakeOrchestrator.calls] == [None, None, None]
    assert [call["run_kwargs"].get("column_platform") for call in _FakeOrchestrator.calls] == ["vm", "vm", "vm"]


def test_session_module_runs_skip_when_platform_setup_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed platform setup skips the session module runs instead of executing them."""

    class _FailingPlatformOrchestrator(_FakeOrchestrator):
        """Fake whose session bracket reports a failed platform setup."""

        def run(self, **kwargs: Any) -> OrchestratorResult:
            type(self).captured.update(kwargs)
            type(self).calls.append(
                {
                    "platform": self.config.tests.platform,
                    "working_dir": self.kwargs.get("working_dir"),
                    "run_kwargs": kwargs,
                }
            )
            hook = kwargs.get("pre_teardown_hook")
            if hook is not None:
                hook({}, False)
            return OrchestratorResult(
                success=False,
                phases=[PhaseResult(phase=Phase.SETUP, success=False, message="setup failed")],
            )

    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FailingPlatformOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FailingPlatformOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--platform", "vm", "--no-upload"])

    # Only the platform config ran; the session modules were skipped, and the
    # overall invocation fails because the platform run failed.
    assert result.exit_code == 1, result.output
    assert [call["platform"] for call in _FailingPlatformOrchestrator.calls] == ["vm"]
    assert result.output.count("platform setup failed, no session available") == 2
    assert result.output.count("[SKIP]") == 2


def test_platform_no_modules_runs_only_platform_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--platform vm --no-modules` runs only the platform config, skipping module fan-out."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app, ["run", "--provider", "acme", "--platform", "vm", "--no-modules", "--no-upload"]
    )

    assert result.exit_code == 0, result.output
    assert [call["platform"] for call in _FakeOrchestrator.calls] == ["vm"]


def test_no_modules_requires_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--no-modules` without `--platform` is rejected."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app, ["run", "--provider", "acme", "--module", "iam", "--no-modules", "--no-upload"]
    )

    assert result.exit_code == 1, result.output
    assert "--no-modules requires --platform." in result.output
    assert _FakeOrchestrator.calls == []


def test_platform_composes_include_labels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--platform vm --label min_req` forwards the include filter into every sub-run."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--label", "min_req", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert all(call["run_kwargs"]["include_labels"] == ["min_req"] for call in _FakeOrchestrator.calls)


def test_platform_dry_run_prints_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A platform dry-run emits the JSON plan and runs nothing."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--dry-run", "--json", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.calls == []
    plan = json.loads(result.output)
    assert plan["provider"] == "acme"
    assert plan["platform"] == "vm"
    assert [r["platform"] for r in plan["runs"]] == ["vm", "iam", "network"]
    # the auto-derived exclude_labels are gone; the plan shows the column instead
    assert [r["column_platform"] for r in plan["runs"]] == ["vm", "vm", "vm"]
    assert all("exclude_labels" not in r for r in plan["runs"])
    # every run in the column uploads the column capability; module runs add their module
    assert [r["upload"] for r in plan["runs"]] == [
        {"capability": "vm", "module": None},
        {"capability": "vm", "module": "iam"},
        {"capability": "vm", "module": "network"},
    ]


def test_platform_dry_run_text_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without --json, the platform dry-run prints a readable plan, not JSON."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--dry-run", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.calls == []
    assert "Dry run: platform column" in result.output
    assert "Platform: vm" in result.output
    assert "Steps:" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_module_dry_run_shows_config_details() -> None:
    """Standalone --module dry-run expands each config like -f --dry-run."""
    security_config = Path(__file__).resolve().parents[1] / "configs" / "providers" / "aws" / "config" / "security.yaml"
    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "aws", "--module", "security", "--dry-run", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert "Dry run: module selection" in result.output
    assert "security.yaml [security]" in result.output
    assert "Steps:" in result.output
    assert "Validations (22):" in result.output
    assert "BmcManagementNetworkCheck" in result.output

    direct = runner.invoke(test_cli.app, ["run", "-f", str(security_config), "--dry-run", "--no-upload"])
    assert direct.exit_code == 0, direct.output
    assert direct.stdout.count("BmcManagementNetworkCheck") == result.stdout.count("BmcManagementNetworkCheck")


def test_module_dispatches_single_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--module iam` runs only the iam module config, no platform column."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--module", "iam", "--no-upload"])

    assert result.exit_code == 0, result.output
    assert [call["platform"] for call in _FakeOrchestrator.calls] == ["iam"]
    assert _FakeOrchestrator.calls[0]["run_kwargs"].get("exclude_labels") is None
    # standalone module runs have no column, hence no platform filtering
    assert _FakeOrchestrator.calls[0]["run_kwargs"].get("column_platform") is None


def _patch_upload(monkeypatch: pytest.MonkeyPatch, upload_calls: list[dict[str, Any]]) -> None:
    """Enable the upload path and capture create_test_run kwargs.

    The fake returns None (creation "failed") so the CLI skips the
    catalog-build/update half of the upload flow after tests run.
    """
    monkeypatch.setattr(test_cli, "check_upload_credentials", lambda: (True, "id", "secret"))
    monkeypatch.setattr(test_cli, "get_environment_config", lambda: ("https://svc.example", "https://issuer.example"))

    def _fake_create_test_run(**kwargs: Any) -> None:
        upload_calls.append(kwargs)
        return None

    monkeypatch.setattr(test_cli, "create_test_run", _fake_create_test_run)


def test_platform_column_uploads_column_capability_for_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Module runs in a --platform column report the column as their capability."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)
    upload_calls: list[dict[str, Any]] = []
    _patch_upload(monkeypatch, upload_calls)

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--platform", "vm", "--lab-id", "7"])

    assert result.exit_code == 0, result.output
    assert [(call["platform"], call["module"]) for call in upload_calls] == [
        ("vm", None),
        ("vm", "iam"),
        ("vm", "network"),
    ]


def test_standalone_module_uploads_module_without_capability(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A standalone --module run reports its module and no capability."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)
    upload_calls: list[dict[str, Any]] = []
    _patch_upload(monkeypatch, upload_calls)

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--module", "iam", "--lab-id", "7"])

    assert result.exit_code == 0, result.output
    assert [(call["platform"], call["module"]) for call in upload_calls] == [(None, "iam")]


def test_config_file_module_suite_uploads_module_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`-f <module config>` reports the module, never the module name as capability."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)
    upload_calls: list[dict[str, Any]] = []
    _patch_upload(monkeypatch, upload_calls)
    config_path = configs_root / "providers" / "acme" / "config" / "iam.yaml"

    result = runner.invoke(test_cli.app, ["run", "-f", str(config_path), "--lab-id", "7"])

    assert result.exit_code == 0, result.output
    assert [(call["platform"], call["module"]) for call in upload_calls] == [(None, "iam")]


def test_config_file_platform_suite_uploads_capability_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`-f <platform config>` reports the platform as capability, no module."""
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)
    _FakeOrchestrator.calls = []
    upload_calls: list[dict[str, Any]] = []
    _patch_upload(monkeypatch, upload_calls)
    config = _write_config(tmp_path)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--lab-id", "7"])

    assert result.exit_code == 0, result.output
    assert [(call["platform"], call["module"]) for call in upload_calls] == [("kubernetes", None)]


def test_platform_and_module_intersect_runs_module_under_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--platform vm --module iam` runs the platform bracket plus only the iam session.

    Under a real column the requested module is a session run, so the platform
    config runs too (its setup provides the session, its teardown comes last);
    the other modules are excluded from the plan.
    """
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--module", "iam", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert [call["platform"] for call in _FakeOrchestrator.calls] == ["vm", "iam"]
    platform_kwargs = _FakeOrchestrator.calls[0]["run_kwargs"]
    assert platform_kwargs.get("pre_teardown_hook") is not None
    module_kwargs = _FakeOrchestrator.calls[1]["run_kwargs"]
    assert module_kwargs.get("column_platform") == "vm"
    assert module_kwargs.get("commands_key") == "iam@vm"
    # the session body receives the platform run's step outputs
    assert module_kwargs.get("session_data") == {"setup": {"success": True}}


def test_platform_and_module_intersect_normalizes_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--platform k8s --module iam` normalizes the column to kubernetes."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    write_axis_suite(configs_root, "k8s.yaml", "kubernetes", "platform")
    write_axis_provider_config(configs_root, "acme", "k8s.yaml", "k8s.yaml", run_platform="kubernetes")
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "k8s", "--module", "iam", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert [call["platform"] for call in _FakeOrchestrator.calls] == ["kubernetes", "iam"]
    module_kwargs = _FakeOrchestrator.calls[1]["run_kwargs"]
    assert module_kwargs.get("column_platform") == "kubernetes"
    assert module_kwargs.get("commands_key") == "iam@kubernetes"


def test_platform_and_module_intersect_uploads_column_module_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An intersect run reports the (column, module) pair on upload."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)
    upload_calls: list[dict[str, Any]] = []
    _patch_upload(monkeypatch, upload_calls)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--module", "iam", "--lab-id", "7"],
    )

    assert result.exit_code == 0, result.output
    # the platform bracket reports (vm, -) and the session module (vm, iam)
    assert [(call["platform"], call["module"]) for call in upload_calls] == [("vm", None), ("vm", "iam")]


def test_platform_and_module_intersect_dry_run_shows_both(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The intersect dry-run plan carries both the platform and the modules."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--module", "iam", "--dry-run", "--json", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOrchestrator.calls == []
    plan = json.loads(result.output)
    assert plan["platform"] == "vm"
    assert plan["modules"] == ["iam"]
    # platform bracket first (provides the session), then only the requested
    # module as a session run; network is not part of the intersect plan
    assert [(r["role"], r["platform"], r["column_platform"]) for r in plan["runs"]] == [
        ("platform", "vm", "vm"),
        ("module", "iam", "vm"),
    ]
    assert [(r["session"], r["commands_key"]) for r in plan["runs"]] == [(False, None), (True, "iam@vm")]
    assert plan["runs"][0]["upload"] == {"capability": "vm", "module": None}
    assert plan["runs"][1]["upload"] == {"capability": "vm", "module": "iam"}

    text = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--module", "iam", "--dry-run", "--no-upload"],
    )
    assert text.exit_code == 0, text.output
    assert "Platform: vm" in text.output
    assert "Modules:  iam" in text.output


def test_platform_column_omits_incompatible_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A module with no column-compatible check is omitted, with the reason surfaced."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    write_axis_suite(configs_root, "foundational.yaml", "foundational", "platform", validations=False)
    write_axis_suite(configs_root, "iam.yaml", "iam", "module", platforms=["foundational"])
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--dry-run", "--json", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert [r["platform"] for r in plan["runs"]] == ["vm", "network"]
    assert [(o["module"], o["reason"]) for o in plan["omitted"]] == [
        ("iam", "no checks compatible with column 'vm'"),
    ]

    text = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--dry-run", "--no-upload"],
    )
    assert text.exit_code == 0, text.output
    assert "omitted: iam (no checks compatible with column 'vm')" in text.output


def test_platform_foundational_column_plans_declared_modules_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--platform foundational` plans a modules-only column of declared modules."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    write_axis_suite(configs_root, "foundational.yaml", "foundational", "platform", validations=False)
    write_axis_suite(configs_root, "iam.yaml", "iam", "module", platforms=["foundational"])
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "foundational", "--dry-run", "--json", "--no-upload"],
    )

    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    # no platform run (the suite wires no validations), only declared modules
    assert [(r["role"], r["platform"], r["column_platform"]) for r in plan["runs"]] == [
        ("module", "iam", "foundational"),
    ]
    assert [o["module"] for o in plan["omitted"]] == ["network"]


def test_no_modules_rejected_with_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--no-modules` contradicts an explicit --module selection."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(
        test_cli.app,
        ["run", "--provider", "acme", "--platform", "vm", "--module", "iam", "--no-modules", "--no-upload"],
    )

    assert result.exit_code == 1, result.output
    assert "--no-modules cannot be combined with --module." in result.output
    assert _FakeOrchestrator.calls == []


def test_platform_requires_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--platform` without `--provider` is rejected."""
    config = _write_config(tmp_path)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--platform", "vm", "-f", str(config), "--no-upload"])

    assert result.exit_code == 1, result.output
    assert "--platform/--module cannot be combined with --config/-f." in result.output
    assert _FakeOrchestrator.calls == []


def test_provider_alone_requires_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--provider` with no label/platform/module names all three selectors."""
    configs_root = tmp_path / "configs"
    _build_platform_provider(configs_root)
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _FakeOrchestrator)

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--no-upload"])

    assert result.exit_code == 1, result.output
    assert "--label" in result.output
    assert "--platform" in result.output
    assert "--module" in result.output
    assert _FakeOrchestrator.calls == []
