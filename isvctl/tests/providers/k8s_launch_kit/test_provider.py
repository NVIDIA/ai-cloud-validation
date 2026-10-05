# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract and framework tests for the generic Kubernetes Launch Kit provider."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from isvtest.core.resolution import State

from isvctl.config.merger import merge_yaml_files
from isvctl.config.output_schemas import validate_output
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.loop import Orchestrator, Phase

_ISVCTL_ROOT = Path(__file__).resolve().parents[3]
_PROVIDERS = _ISVCTL_ROOT / "configs" / "providers"
_LAUNCH_KIT_PROVIDER = _PROVIDERS / "k8s-launch-kit"
_PROVIDER = _LAUNCH_KIT_PROVIDER / "scripts" / "adapter.py"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_MOCK_L8K = _FIXTURES / "mock_l8k.py"
_NETWORK_OPERATOR_CONFIG = _LAUNCH_KIT_PROVIDER / "config" / "network-operator.yaml"
_FAMILIES = ("ICMPPing", "RDMAPing", "IBWriteBandwidth", "DMABufBandwidth")
_CATALOG_TESTS = [
    "K8sNetworkOperatorDeployment",
    *(f"K8sEastWestNetwork{family}-{fabric}" for family in _FAMILIES for fabric in ("ethernet", "infiniband")),
]


def _load_provider_module() -> ModuleType:
    """Load the provider script for isolated installer tests."""
    spec = importlib.util.spec_from_file_location("k8s_launch_kit_provider", _PROVIDER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {_PROVIDER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_provider(
    *arguments: str,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run one provider operation from the same directory used by isvctl."""
    completed = subprocess.run(
        [sys.executable, str(_PROVIDER), *arguments],
        cwd=_PROVIDERS,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"provider emitted non-JSON stdout (exit {completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        ) from error
    assert isinstance(output, dict)
    return completed, output


def _run_workflow(
    command: str,
    arguments: list[str],
    *,
    working_dir: Path,
    artifact_dir: Path,
    user_config: Path | None = None,
    deployment_files: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run a mocked l8k workflow command through the generic transport."""
    provider_arguments = [
        "run",
        "--executable",
        str(_MOCK_L8K),
        "--command",
        command,
        "--arguments-json",
        json.dumps(arguments),
        "--environment-json",
        "{}",
        "--working-dir",
        str(working_dir),
        "--artifact-dir",
        str(artifact_dir),
    ]
    if user_config is not None:
        provider_arguments.extend(["--user-config", str(user_config)])
    if deployment_files is not None:
        provider_arguments.extend(["--deployment-files", str(deployment_files)])
    return _run_provider(*provider_arguments, env=env)


def _recorded_argv(output: dict[str, Any]) -> list[str]:
    """Return the l8k argv retained in the command evidence."""
    return json.loads(Path(output["artifacts"]["command"]).read_text(encoding="utf-8"))["argv"]


def _recorded_documents(output: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the l8k JSON documents retained in the stdout evidence."""
    stdout = Path(output["artifacts"]["stdout"]).read_text(encoding="utf-8")
    return _load_provider_module()._parse_json_stream(stdout, "stdout")


def _mocked_network_operator_config(tmp_path: Path) -> RunConfig:
    """Load production wiring, then inject test-owned executables and paths."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    context = merged["context"]["k8s_launch_kit"]
    user_config = tmp_path / "cluster-config.yaml"
    user_config.write_text(
        """networkOperator:
  selectedRelease: "26.4"
profile:
  fabric: ethernet
  deployment: sriov
clusterConfig: []
""",
        encoding="utf-8",
    )
    deployment_files = tmp_path / "deployment"
    deployment_files.mkdir()
    context["executable"] = str(_MOCK_L8K)
    context["user_config"] = str(user_config)
    context["deployment_files"] = str(deployment_files)
    context["working_dir"] = str(tmp_path / "work")
    context["artifact_dir"] = str(tmp_path / "evidence")
    return RunConfig.model_validate(merged)


def test_network_operator_provider_defaults_to_real_cli_tools() -> None:
    """The shipped provider validates once and always collects diagnostics."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    config = RunConfig.model_validate(merged)
    context = merged["context"]["k8s_launch_kit"]

    assert context == {
        "executable": "l8k",
        "user_config": "",
        "deployment_files": "",
        "working_dir": "../../../../../_output/k8s-launch-kit/network-operator/work",
        "artifact_dir": "../../../../../_output/k8s-launch-kit/network-operator/evidence",
        "environment": {},
    }
    assert "mock" not in json.dumps(merged).lower()
    assert "poc" not in json.dumps(merged).lower()
    command = config.commands["network_operator"]
    assert command.phases == ["test"]
    assert [step.name for step in command.steps] == ["launch_kit_validate", "launch_kit_sosreport"]
    validate_step, sosreport_step = command.steps
    assert validate_step.timeout is None
    assert "--user-config={{ context.k8s_launch_kit.user_config }}" in validate_step.args
    assert "--deployment-files={{ context.k8s_launch_kit.deployment_files }}" in validate_step.args
    assert sosreport_step.timeout == 1800
    assert sosreport_step.phase == "test"
    assert sosreport_step.finalizer_for == "launch_kit_validate"
    assert sosreport_step.requires == validate_step.requires


def test_network_operator_suite_matches_the_static_catalog() -> None:
    """One deployment test plus one test per connectivity family and fabric."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    checks = merged["tests"]["validations"]["network_operator"]["checks"]

    assert list(checks) == _CATALOG_TESTS
    for name, params in checks.items():
        if name.startswith("K8sEastWestNetwork"):
            assert params["fabric"] == name.rsplit("-", 1)[1]


def test_launch_kit_executable_resolves_from_path(tmp_path: Path, monkeypatch: Any) -> None:
    """The production `l8k` setting is resolved as a normal executable."""
    module = _load_provider_module()
    executable = tmp_path / "l8k"
    executable.write_text("test executable", encoding="utf-8")
    monkeypatch.setattr(module.shutil, "which", lambda value: str(executable) if value == "l8k" else None)

    assert module._resolve_executable("l8k") == executable.resolve()


def test_sosreport_preserves_text_output_and_registers_its_directory(tmp_path: Path) -> None:
    """The adapter retains the text-only sosreport contract as structured evidence."""
    working_dir = tmp_path / "work"
    artifact_dir = tmp_path / "evidence"

    completed, output = _run_workflow(
        "sosreport",
        [],
        working_dir=working_dir,
        artifact_dir=artifact_dir,
    )

    assert completed.returncode == 0
    assert output["success"] is True
    assert output["operation"] == "sosreport"
    assert _recorded_argv(output)[1:] == ["sosreport", "--output-dir", str(artifact_dir / "sosreport")]
    assert output["artifacts"]["sosreport"] == str(artifact_dir / "sosreport")
    assert (artifact_dir / "sosreport" / "network-operator-sosreport.tar.gz").is_file()
    assert "Sosreport collected" in Path(output["artifacts"]["stdout"]).read_text(encoding="utf-8")
    assert validate_output(output, "k8s_launch_kit") == (True, [])


def test_network_operator_provider_runs_validate_then_sosreport(tmp_path: Path) -> None:
    """The production configuration validates once and then collects diagnostics."""
    config = _mocked_network_operator_config(tmp_path)

    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
    )

    assert result.success is True
    assert list(result.inventory) == ["launch_kit_validate", "launch_kit_sosreport"]
    assert [phase.name for phase in result.phases] == ["test", "test-teardown"]
    validations = {validation.entry.name: validation for validation in result.validations}
    assert list(validations) == _CATALOG_TESTS
    for name, validation in validations.items():
        expected = State.SKIPPED if name.endswith("-infiniband") else State.PASSED
        assert validation.state is expected, name
    ethernet = [validation for name, validation in validations.items() if name.endswith("-ethernet")]
    assert sum(validation.subtest_summary.passed for validation in ethernet) == 32
    assert all(validation.subtest_summary.failed == 0 for validation in validations.values())

    argv = _recorded_argv(result.inventory["launch_kit_validate"])
    assert argv[1] == "validate"
    assert argv[argv.index("--user-config") + 1] == str((tmp_path / "cluster-config.yaml").resolve())
    assert argv[argv.index("--deployment-files") + 1] == str((tmp_path / "deployment").resolve())
    assert argv[-2:] == ["--output", "json"]
    report = tmp_path / "evidence" / "k8s-launch-kit-validation-report.html"
    assert result.inventory["launch_kit_validate"]["artifacts"]["validation_report"] == str(report)
    assert report.is_file()
    sosreport = result.inventory["launch_kit_sosreport"]
    assert _recorded_argv(sosreport)[1:] == ["sosreport", "--output-dir", str(tmp_path / "evidence" / "sosreport")]
    assert Path(sosreport["artifacts"]["sosreport"]).is_dir()


def test_sosreport_failure_does_not_replace_connectivity_result(tmp_path: Path, monkeypatch: Any) -> None:
    """Diagnostic failure is separate while the connectivity assertion stays passed."""
    monkeypatch.setenv("L8K_MOCK_FAIL", "sosreport")
    config = _mocked_network_operator_config(tmp_path)

    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
    )

    assert result.success is False
    assert result.validations[0].state is State.PASSED
    assert [(phase.name, phase.success) for phase in result.phases] == [
        ("test", True),
        ("test-teardown", False),
    ]
    assert result.inventory["launch_kit_sosreport"]["success"] is False
    assert "sosreport collection failed" in result.inventory["launch_kit_sosreport"]["error"]


def test_network_operator_provider_expands_input_paths(tmp_path: Path, monkeypatch: Any) -> None:
    """Tilde inputs are resolved before they are supplied to Launch Kit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    user_config = tmp_path / "l8k" / "cluster-config.yaml"
    user_config.parent.mkdir()
    user_config.write_text(
        "profile:\n  fabric: ethernet\n  deployment: sriov\n",
        encoding="utf-8",
    )
    deployment_files = tmp_path / "l8k" / "deployment"
    deployment_files.mkdir()

    completed, output = _run_workflow(
        "validate",
        [],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=Path("~/l8k/cluster-config.yaml"),
        deployment_files=Path("~/l8k/deployment"),
    )

    assert completed.returncode == 0
    argv = _recorded_argv(output)
    assert argv[argv.index("--user-config") + 1] == str(user_config)
    assert argv[argv.index("--deployment-files") + 1] == str(deployment_files)


@pytest.mark.parametrize(
    ("user_config", "deployment_files", "expected"),
    [
        (None, "deployment", "user_config is required"),
        ("cluster-config.yaml", None, "--user-config requires --deployment-files"),
    ],
)
def test_validate_requires_both_prerequisite_inputs(
    tmp_path: Path,
    user_config: str | None,
    deployment_files: str | None,
    expected: str,
) -> None:
    """Partial prerequisite input fails before Launch Kit execution."""
    config_path = tmp_path / "cluster-config.yaml"
    config_path.write_text("profile: {}\n", encoding="utf-8")
    deployment_path = tmp_path / "deployment"
    deployment_path.mkdir()

    completed, output = _run_workflow(
        "validate",
        [],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=config_path if user_config is not None else None,
        deployment_files=deployment_path if deployment_files is not None else None,
    )

    assert completed.returncode == 1
    assert output["success"] is False
    assert expected in output["error"]


def test_validate_rejects_duplicate_path_flags(tmp_path: Path) -> None:
    """Dedicated inputs cannot silently conflict with raw Launch Kit arguments."""
    user_config = tmp_path / "cluster-config.yaml"
    user_config.write_text("profile: {}\n", encoding="utf-8")
    deployment_files = tmp_path / "deployment"
    deployment_files.mkdir()

    completed, output = _run_workflow(
        "validate",
        ["--user-config", "other.yaml"],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=user_config,
        deployment_files=deployment_files,
    )

    assert completed.returncode == 1
    assert "cannot be combined with raw flag(s): --user-config" in output["error"]


def test_failed_connectivity_is_a_junit_failure(tmp_path: Path, monkeypatch: Any) -> None:
    """A failed Launch Kit matrix row is retained as a test failure in JUnit."""
    monkeypatch.setenv("L8K_MOCK_FAIL", "validate:ib_write_bw")
    config = _mocked_network_operator_config(tmp_path)
    junit_path = tmp_path / "junit.xml"

    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
        junitxml=str(junit_path),
    )

    assert result.success is False
    assert list(result.inventory) == ["launch_kit_validate", "launch_kit_sosreport"]
    report = tmp_path / "evidence" / "k8s-launch-kit-validation-report.html"
    assert result.inventory["launch_kit_validate"]["artifacts"]["validation_report"] == str(report)
    assert report.is_file()
    assert (tmp_path / "evidence" / "sosreport" / "network-operator-sosreport.tar.gz").is_file()
    states = {validation.entry.name: validation.state for validation in result.validations}
    assert states["K8sEastWestNetworkIBWriteBandwidth-ethernet"] is State.FAILED
    assert states["K8sEastWestNetworkICMPPing-ethernet"] is State.PASSED
    # The failed family explains the l8k exit code, so the deployment test stays green.
    assert states["K8sNetworkOperatorDeployment"] is State.PASSED
    failed = next(v for v in result.validations if v.entry.name == "K8sEastWestNetworkIBWriteBandwidth-ethernet")
    assert failed.subtest_summary.failed == 1
    case = next(
        case
        for case in ET.parse(junit_path).getroot().iter("testcase")
        if case.get("name") == "K8sEastWestNetworkIBWriteBandwidth-ethernet"
    )
    assert case.find("failure") is not None
    assert case.find("error") is None
    assert case.find("skipped") is None


def test_missing_prerequisites_are_a_step_error(tmp_path: Path) -> None:
    """An unset prerequisite produces an actionable validation error."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    merged["context"]["k8s_launch_kit"]["executable"] = str(_MOCK_L8K)
    merged["context"]["k8s_launch_kit"]["working_dir"] = str(tmp_path / "work")
    merged["context"]["k8s_launch_kit"]["artifact_dir"] = str(tmp_path / "evidence")
    config = RunConfig.model_validate(merged)

    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
    )

    assert result.success is False
    assert list(result.inventory) == ["launch_kit_validate", "launch_kit_sosreport"]
    assert (tmp_path / "evidence" / "sosreport" / "network-operator-sosreport.tar.gz").is_file()
    assert result.validations[0].state is State.FAILED
    assert "user_config is required" in result.validations[0].message


def test_failed_validate_preserves_documents_and_process_error(tmp_path: Path) -> None:
    """A non-zero l8k result retains every JSON document and a clear exit diagnostic."""
    user_config = tmp_path / "cluster-config.yaml"
    user_config.write_text("profile:\n  fabric: ethernet\n  deployment: sriov\n", encoding="utf-8")
    deployment_files = tmp_path / "deployment"
    deployment_files.mkdir()
    env = os.environ.copy()
    env["L8K_MOCK_FAIL"] = "validate:ib_write_bw"

    completed, output = _run_workflow(
        "validate",
        [],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=user_config,
        deployment_files=deployment_files,
        env=env,
    )

    assert completed.returncode == 4
    assert output["success"] is False
    assert len(_recorded_documents(output)) == 3
    assert "l8k validate exited with code 4" in output["error"]
    assert Path(output["artifacts"]["validation_report"]).is_file()
    assert Path(output["artifacts"]["stdout"]).read_text(encoding="utf-8")


def test_missing_advertised_validation_report_is_an_evidence_error(tmp_path: Path) -> None:
    """A stale report cannot satisfy a new Launch Kit reportPath document."""
    missing_report = tmp_path / "missing-validation-report.html"
    executable = tmp_path / "l8k"
    executable.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{{\"reportPath\":\"{missing_report}\"}}'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    artifact_dir = tmp_path / "evidence"
    retained_report = artifact_dir / "k8s-launch-kit-validation-report.html"
    retained_report.parent.mkdir(parents=True)
    retained_report.write_text("stale report\n", encoding="utf-8")

    completed, output = _run_provider(
        "run",
        "--executable",
        str(executable),
        "--command",
        "validate",
        "--arguments-json",
        "[]",
        "--working-dir",
        str(tmp_path / "work"),
        "--artifact-dir",
        str(artifact_dir),
    )

    assert completed.returncode == 1
    assert output["success"] is False
    assert "failed to retain Launch Kit HTML validation report" in output["error"]
    assert str(missing_report) in output["error"]
    assert "validation_report" not in output["artifacts"]
    assert not retained_report.exists()


def test_catalog_names_reach_standard_junit(tmp_path: Path) -> None:
    """The report uploaded by isvctl has one testcase per catalog test; the other fabric skips."""
    config = _mocked_network_operator_config(tmp_path)
    junit = tmp_path / "junit-validation.xml"
    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
        junitxml=str(junit),
    )
    assert result.success
    cases = {case.get("name"): case for case in ET.parse(junit).getroot().iter("testcase")}
    assert set(_CATALOG_TESTS) <= set(cases)
    assert "K8sEastWestNetworkICMPPing-ethernet::probe-0" in cases
    assert not any(f"{name}::{name}::" in case_name for name in _CATALOG_TESTS for case_name in cases)
    for name in _CATALOG_TESTS:
        skipped = cases[name].find("skipped")
        if name.endswith("-infiniband"):
            assert skipped is not None
            assert "Cluster fabric is not configured for this fabric type: infiniband" in skipped.get("message", "")
        else:
            assert skipped is None
    native = result.inventory["launch_kit_validate"]["artifacts"]["validation_junit"]
    assert [suite.get("name") for suite in ET.parse(native).getroot().findall("testsuite")] == [
        "network/validation",
        *(f"K8sEastWestNetwork{family}-ethernet" for family in _FAMILIES),
    ]


def test_validate_cannot_reuse_stale_junit(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "evidence"
    artifact_dir.mkdir()
    (artifact_dir / "launch-kit-junit.xml").write_text("<testsuites/>")
    executable = tmp_path / "l8k"
    executable.write_text("#!/bin/sh\nprintf '{}\\n'\n")
    executable.chmod(0o755)
    completed, output = _run_provider(
        "run",
        "--executable",
        str(executable),
        "--command",
        "validate",
        "--arguments-json",
        "[]",
        "--working-dir",
        str(tmp_path / "work"),
        "--artifact-dir",
        str(artifact_dir),
    )
    assert completed.returncode == 1
    assert not output["success"]
    assert "--junit-path" in output["error"]
    assert "validation_junit" not in output["artifacts"]
    assert not (artifact_dir / "launch-kit-junit.xml").exists()


@pytest.mark.parametrize("arguments", [["--junit-path", "other.xml"], ["--junit-path=other.xml"]])
def test_junit_output_path_is_provider_owned(tmp_path: Path, arguments: list[str]) -> None:
    completed, output = _run_workflow(
        "validate", arguments, working_dir=tmp_path / "work", artifact_dir=tmp_path / "evidence"
    )
    assert completed.returncode == 1
    assert "--junit-path is managed" in output["error"]
