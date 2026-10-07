# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The network suite's network_operator group, run end to end against a mock ``l8k``."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from isvtest.core.resolution import State

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.loop import Orchestrator, OrchestratorResult, Phase

_REPO = Path(__file__).resolve().parents[2]
_NETWORK_SUITE = _REPO / "isvctl" / "configs" / "suites" / "network.yaml"
_MOCK_L8K = _REPO / "isvtest" / "tests" / "k8s_launch_kit" / "fixtures" / "mock_l8k.py"
_FAMILIES = ("ICMPPing", "RDMAPing", "IBWriteBandwidth", "DMABufBandwidth")
_CATALOG_TESTS = [
    "NetworkOperatorDeployment",
    *(f"EastWestNetwork{family}-{fabric}" for family in _FAMILIES for fabric in ("ethernet", "infiniband")),
]


@pytest.fixture(autouse=True)
def _mock_launch_kit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the mock on PATH as ``l8k`` and keep evidence under ``tmp_path``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "l8k").symlink_to(_MOCK_L8K)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.chdir(tmp_path)


def _run(tmp_path: Path, *, configured: bool = True, junitxml: Path | None = None) -> OrchestratorResult:
    merged = merge_yaml_files([_NETWORK_SUITE])
    if configured:
        user_config = tmp_path / "cluster-config.yaml"
        user_config.write_text("profile:\n  fabric: ethernet\n  deployment: sriov\n", encoding="utf-8")
        (tmp_path / "deployment").mkdir(exist_ok=True)
        merged["tests"]["settings"]["k8s_launch_kit"] = {
            "user_config": str(user_config),
            "deployment_files": str(tmp_path / "deployment"),
        }
    return Orchestrator(RunConfig.model_validate(merged), working_dir=_NETWORK_SUITE.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
        include_labels=["network_operator"],
        junitxml=str(junitxml) if junitxml else None,
    )


def _network_operator(result: OrchestratorResult) -> dict[str, State]:
    return {v.entry.name: v.state for v in result.validations if "network_operator" in v.entry.labels}


def test_one_launch_kit_run_feeds_every_catalog_test(tmp_path: Path) -> None:
    """The configured fabric's tests pass; the other fabric's four skip."""
    junit = tmp_path / "junit-validation.xml"

    result = _run(tmp_path, junitxml=junit)

    assert result.success is True
    states = _network_operator(result)
    assert list(states) == [*_CATALOG_TESTS, "LaunchKitSosreport"]
    assert states.pop("LaunchKitSosreport") is State.PASSED
    for name, state in states.items():
        assert state is (State.SKIPPED if name.endswith("-infiniband") else State.PASSED), name
    commands = tmp_path / "_output" / "k8s-launch-kit" / "commands"
    assert sorted(path.name for path in commands.iterdir()) == ["sosreport", "validate"]

    cases = {case.get("name"): case for case in ET.parse(junit).getroot().iter("testcase")}
    assert set(_CATALOG_TESTS) <= set(cases)
    assert "EastWestNetworkICMPPing-ethernet::probe-0" in cases
    for name in _CATALOG_TESTS:
        skipped = cases[name].find("skipped")
        if name.endswith("-infiniband"):
            assert skipped is not None
            assert "Cluster fabric is not configured for this fabric type: infiniband" in skipped.get("message", "")
        else:
            assert skipped is None


def test_failed_family_is_a_junit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("L8K_MOCK_FAIL", "validate:ib_write_bw")
    junit = tmp_path / "junit-validation.xml"

    result = _run(tmp_path, junitxml=junit)

    assert result.success is False
    states = _network_operator(result)
    assert states["EastWestNetworkIBWriteBandwidth-ethernet"] is State.FAILED
    assert states["EastWestNetworkICMPPing-ethernet"] is State.PASSED
    # The failed family explains the l8k exit code, so the deployment test stays green.
    assert states["NetworkOperatorDeployment"] is State.PASSED
    assert (tmp_path / "_output" / "k8s-launch-kit" / "sosreport.tar.gz").is_file()
    cases = {case.get("name"): case for case in ET.parse(junit).getroot().iter("testcase")}
    assert cases["EastWestNetworkIBWriteBandwidth-ethernet"].find("failure") is not None


def test_unconfigured_inputs_skip_every_test(tmp_path: Path) -> None:
    """Running the network suite without Launch Kit inputs never calls l8k."""
    result = _run(tmp_path, configured=False)

    assert set(_network_operator(result).values()) == {State.SKIPPED}
    assert not (tmp_path / "_output").exists()


def test_failed_sosreport_fails_the_run_without_changing_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostics are evidence: a failed collection fails the run, not a catalog test."""
    monkeypatch.setenv("L8K_MOCK_FAIL", "sosreport")

    result = _run(tmp_path)

    assert result.success is False
    states = _network_operator(result)
    assert states.pop("LaunchKitSosreport") is State.FAILED
    for name, state in states.items():
        assert state is (State.SKIPPED if name.endswith("-infiniband") else State.PASSED), name


def test_missing_launch_kit_skips_every_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured inputs but no l8k: nothing could run, so nothing fails."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    result = _run(tmp_path)

    assert result.success is True
    assert set(_network_operator(result).values()) == {State.SKIPPED}
    assert not (tmp_path / "_output" / "k8s-launch-kit" / "commands").exists()


def test_each_run_checks_the_cluster_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second orchestration in the same process runs Launch Kit again."""
    assert _run(tmp_path).success is True

    monkeypatch.setenv("L8K_MOCK_FAIL", "validate:ib_write_bw")
    result = _run(tmp_path)

    assert result.success is False
    assert _network_operator(result)["EastWestNetworkIBWriteBandwidth-ethernet"] is State.FAILED
