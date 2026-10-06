# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The shared Launch Kit run behind the Network Operator checks, against a mock ``l8k``."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from isvtest.validations.k8s_launch_kit import runner

pytestmark = pytest.mark.unit

_MOCK_L8K = Path(__file__).resolve().parent / "fixtures" / "mock_l8k.py"


@pytest.fixture(autouse=True)
def _fresh_runs() -> None:
    runner.clear_runs()


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    user_config = tmp_path / "cluster-config.yaml"
    user_config.write_text("profile:\n  fabric: ethernet\n  deployment: sriov\n", encoding="utf-8")
    deployment_files = tmp_path / "deployment"
    deployment_files.mkdir(exist_ok=True)
    return user_config, deployment_files


def _run(tmp_path: Path, executable: Path = _MOCK_L8K, **inputs: str) -> dict[str, Any]:
    if not inputs:
        user_config, deployment_files = _inputs(tmp_path)
        inputs = {"user_config": str(user_config), "deployment_files": str(deployment_files)}
    return runner.run_launch_kit(executable=str(executable), artifact_dir=tmp_path / "evidence", **inputs)


def _argv(result: dict[str, Any]) -> list[str]:
    return json.loads(Path(result["artifacts"]["command"]).read_text(encoding="utf-8"))["argv"]


def test_validate_then_sosreport_retain_evidence(tmp_path: Path) -> None:
    """One validate run binds both inputs and its own JUnit path; sosreport follows."""
    result = _run(tmp_path)

    assert result["success"] is True
    argv = _argv(result)
    assert argv[1] == "validate"
    assert argv[argv.index("--user-config") + 1] == str(tmp_path / "cluster-config.yaml")
    assert argv[argv.index("--deployment-files") + 1] == str(tmp_path / "deployment")
    assert argv[argv.index("--junit-path") + 1] == str(tmp_path / "evidence" / "launch-kit-junit.xml")
    assert argv[-2:] == ["--output", "json"]
    assert Path(result["artifacts"]["validation_report"]).is_file()
    assert ET.parse(result["artifacts"]["validation_junit"]).getroot().tag == "testsuites"

    sosreport = result["sosreport"]
    assert sosreport["success"] is True
    assert _argv(sosreport)[1:] == ["sosreport", "--output-dir", str(tmp_path / "evidence" / "sosreport")]
    assert sosreport["artifacts"]["sosreport"] == str(tmp_path / "evidence" / "sosreport.tar.gz")
    assert (tmp_path / "evidence" / "sosreport.tar.gz").is_file()


def test_runs_once_per_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every check of one session shares the same Launch Kit run."""
    calls: list[str] = []
    validate = runner._validate
    monkeypatch.setattr(runner, "_validate", lambda *args: calls.append("validate") or validate(*args))

    first = _run(tmp_path)
    assert _run(tmp_path) is first
    assert calls == ["validate"]


def test_executable_resolves_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "l8k"
    executable.write_text("test executable", encoding="utf-8")
    monkeypatch.setattr(runner.shutil, "which", lambda value: str(executable) if value == "l8k" else None)

    assert runner._resolve_executable("l8k") == executable.resolve()


def test_expands_input_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tilde inputs are resolved before they are supplied to Launch Kit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _inputs(tmp_path)

    result = _run(tmp_path, user_config="~/cluster-config.yaml", deployment_files="~/deployment")

    argv = _argv(result)
    assert argv[argv.index("--user-config") + 1] == str(tmp_path / "cluster-config.yaml")
    assert argv[argv.index("--deployment-files") + 1] == str(tmp_path / "deployment")


@pytest.mark.parametrize(
    ("user_config", "deployment_files", "expected"),
    [
        ("", "deployment", "user_config is required"),
        ("cluster-config.yaml", "", "deployment_files is required"),
        ("missing.yaml", "deployment", "user config not found"),
        ("cluster-config.yaml", "missing", "deployment directory not found"),
    ],
)
def test_missing_inputs_skip_both_commands(
    tmp_path: Path, user_config: str, deployment_files: str, expected: str
) -> None:
    """Launch Kit cannot start without both inputs, so neither command runs."""
    _inputs(tmp_path)
    inputs = {
        "user_config": str(tmp_path / user_config) if user_config else "",
        "deployment_files": str(tmp_path / deployment_files) if deployment_files else "",
    }

    result = _run(tmp_path, **inputs)

    assert expected in result["skip_reason"]
    assert "error" not in result
    assert result["sosreport"]["skip_reason"] == result["skip_reason"]
    assert not (tmp_path / "evidence" / "commands").exists()


def test_failed_validate_keeps_documents_and_exit_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("L8K_MOCK_FAIL", "validate:ib_write_bw")

    result = _run(tmp_path)

    assert result["success"] is False
    assert "l8k validate exited with code 4" in result["error"]
    stdout = Path(result["artifacts"]["stdout"]).read_text(encoding="utf-8")
    assert len(runner._parse_json_stream(stdout, "stdout")) == 3
    assert Path(result["artifacts"]["validation_report"]).is_file()
    assert result["sosreport"]["success"] is True


def test_failed_sosreport_leaves_validate_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("L8K_MOCK_FAIL", "sosreport")

    result = _run(tmp_path)

    assert result["success"] is True
    assert result["sosreport"]["success"] is False
    assert "sosreport collection failed" in result["sosreport"]["error"]
    # Without an archive, the partial collection directory is the evidence.
    assert result["sosreport"]["artifacts"]["sosreport"] == str(tmp_path / "evidence" / "sosreport")


def test_sosreport_cannot_reuse_stale_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = tmp_path / "evidence" / "sosreport.tar.gz"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale archive\n")
    monkeypatch.setenv("L8K_MOCK_FAIL", "sosreport")

    result = _run(tmp_path)

    assert not stale.exists()
    assert result["sosreport"]["artifacts"]["sosreport"] != str(stale)


def test_missing_advertised_report_is_an_evidence_error(tmp_path: Path) -> None:
    """A stale report cannot satisfy a new Launch Kit reportPath document."""
    missing_report = tmp_path / "missing-validation-report.html"
    executable = tmp_path / "l8k"
    executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{{\"reportPath\":\"{missing_report}\"}}'\n", encoding="utf-8")
    executable.chmod(0o755)
    retained_report = tmp_path / "evidence" / "k8s-launch-kit-validation-report.html"
    retained_report.parent.mkdir(parents=True)
    retained_report.write_text("stale report\n", encoding="utf-8")

    result = _run(tmp_path, executable)

    assert result["success"] is False
    assert "failed to retain Launch Kit HTML validation report" in result["error"]
    assert str(missing_report) in result["error"]
    assert "validation_report" not in result["artifacts"]
    assert not retained_report.exists()


def test_validate_cannot_reuse_stale_junit(tmp_path: Path) -> None:
    stale = tmp_path / "evidence" / "launch-kit-junit.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text("<testsuites/>")
    executable = tmp_path / "l8k"
    executable.write_text("#!/bin/sh\nprintf '{}\\n'\n")
    executable.chmod(0o755)

    result = _run(tmp_path, executable)

    assert result["success"] is False
    assert "--junit-path" in result["error"]
    assert "validation_junit" not in result["artifacts"]
    assert not stale.exists()


def test_missing_executable_skips_both_commands(tmp_path: Path) -> None:
    result = _run(tmp_path, tmp_path / "missing-l8k")

    assert "Launch Kit executable not found" in result["skip_reason"]
    assert "error" not in result
    assert result["sosreport"]["skip_reason"] == result["skip_reason"]
