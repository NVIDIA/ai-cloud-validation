# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native Launch Kit JUnit suites reported as static catalog tests."""

from pathlib import Path

import pytest

from isvtest.core.validation import BaseValidation
from isvtest.validations.k8s_launch_kit import checks
from isvtest.validations.k8s_launch_kit.checks import (
    K8sEastWestNetworkRDMAPing,
    K8sNetworkOperatorDeployment,
    LaunchKitSosreport,
)

pytestmark = pytest.mark.unit

_PASSING_RDMA = """<testsuite name="K8sEastWestNetworkRDMAPing-ethernet">
  <testcase name="K8sEastWestNetworkRDMAPing-ethernet::a→b" classname="network.connectivity"/>
</testsuite>"""


_INPUTS = {"user_config": "cluster-config.yaml", "deployment_files": "deployment"}
_SOSREPORT = {"success": True, "artifacts": {"sosreport": "/evidence/sosreport.tar.gz"}}


@pytest.fixture(autouse=True)
def _launch_kit(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stand in for the shared Launch Kit run; each test sets what it returned."""
    output: dict = {}
    monkeypatch.setattr(checks, "run_launch_kit", lambda *_, **__: output)
    return output


def _execute(
    tmp_path: Path,
    suites: str,
    check: type[BaseValidation] = K8sEastWestNetworkRDMAPing,
    *,
    fabric: str = "ethernet",
    success: bool = True,
    sosreport: dict = _SOSREPORT,
) -> dict:
    report = tmp_path / "junit.xml"
    report.write_text(f"<testsuites>{suites}</testsuites>")
    checks.run_launch_kit().update(
        {
            "success": success,
            "error": "process failed",
            "artifacts": {"validation_junit": str(report)},
            "sosreport": sosreport,
        }
    )
    return check(config={"fabric": fabric, **_INPUTS}).execute()


def test_family_reports_only_its_native_suite(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        """
    <testsuite name="K8sEastWestNetworkRDMAPing-ethernet">
      <testcase name="K8sEastWestNetworkRDMAPing-ethernet::a→b" classname="network.connectivity" time="0.125">
        <system-out>srcGPU=2 bandwidthGbps=187.6</system-out>
      </testcase>
    </testsuite>
    <testsuite name="K8sEastWestNetworkICMPPing-ethernet">
      <testcase name="K8sEastWestNetworkICMPPing-ethernet::a→b"><failure message="unreachable"/></testcase>
    </testsuite>""",
    )
    assert result["passed"]
    [subtest] = result["subtests"]
    assert subtest["name"] == "a→b"
    assert subtest["duration"] == 0.125
    assert "bandwidthGbps=187.6" in subtest["message"]


@pytest.mark.parametrize("tag", ["failure", "error"])
def test_family_failures_keep_evidence(tmp_path: Path, tag: str) -> None:
    result = _execute(
        tmp_path,
        f"""<testsuite name="K8sEastWestNetworkRDMAPing-ethernet">
      <testcase name="probe" classname="network.connectivity">
        <{tag} message="bandwidth below threshold">42.5 &lt; 100</{tag}>
        <system-err>connection refused</system-err>
      </testcase></testsuite>""",
    )
    assert not result["passed"]
    assert "42.5 < 100" in result["error"]
    assert "connection refused" in result["error"]
    assert not result["subtests"][0]["passed"]


def test_other_fabric_skips(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match="not configured for this fabric type: infiniband"):
        _execute(tmp_path, _PASSING_RDMA, fabric="infiniband")


def test_disabled_family_skips_with_native_reason(tmp_path: Path) -> None:
    disabled = """<testsuite name="K8sEastWestNetworkRDMAPing-ethernet">
      <testcase name="K8sEastWestNetworkRDMAPing-ethernet" classname="network.connectivity">
        <skipped message="Check disabled by validation configuration"/>
      </testcase></testsuite>"""
    with pytest.raises(pytest.skip.Exception, match="Check disabled by validation configuration"):
        _execute(tmp_path, disabled)


def test_family_passes_when_another_family_failed_the_run(tmp_path: Path) -> None:
    assert _execute(tmp_path, _PASSING_RDMA, success=False)["passed"]


def test_invalid_fabric_fails(tmp_path: Path) -> None:
    assert not _execute(tmp_path, _PASSING_RDMA, fabric="roce")["passed"]


@pytest.mark.parametrize("check", [K8sEastWestNetworkRDMAPing, K8sNetworkOperatorDeployment])
@pytest.mark.parametrize("contents", [None, "<broken", "<testcase/>"])
def test_missing_or_malformed_xml_fails(tmp_path: Path, check: type[BaseValidation], contents: str | None) -> None:
    report = tmp_path / "junit.xml"
    if contents is not None:
        report.write_text(contents)
    checks.run_launch_kit().update({"success": True, "artifacts": {"validation_junit": str(report)}})
    result = check(config={"fabric": "ethernet", **_INPUTS}).execute()
    assert not result["passed"]


@pytest.mark.parametrize("duration", ["NaN", "-1", "Infinity", "invalid"])
def test_invalid_duration_fails(tmp_path: Path, duration: str) -> None:
    suite = f"""<testsuite name="K8sEastWestNetworkRDMAPing-ethernet">
      <testcase name="probe" time="{duration}"/></testsuite>"""
    assert not _execute(tmp_path, suite)["passed"]


def test_deployment_reports_validation_suite(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        """<testsuite name="network/validation">
      <testcase name="NetworkOperatorVersion" classname="network.validation"/>
      <testcase name="IPPool/ns/rail-0" classname="network.validation"/>
    </testsuite>""",
        K8sNetworkOperatorDeployment,
    )
    assert result["passed"]
    assert [subtest["name"] for subtest in result["subtests"]] == ["NetworkOperatorVersion", "IPPool/ns/rail-0"]


def test_deployment_execution_error_fails(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        """<testsuite name="network/validation">
      <testcase name="ValidationExecution" classname="network.validation"><error message="setup failed"/></testcase>
    </testsuite>""",
        K8sNetworkOperatorDeployment,
        success=False,
    )
    assert not result["passed"]
    assert "setup failed" in result["error"]


@pytest.mark.parametrize(
    "suites", ["", '<testsuite name="network/validation"><testcase name="HelmValues"/></testsuite>']
)
def test_deployment_owns_unexplained_process_failure(tmp_path: Path, suites: str) -> None:
    result = _execute(tmp_path, suites + _PASSING_RDMA, K8sNetworkOperatorDeployment, success=False)
    assert not result["passed"]
    assert result["error"] == "process failed"


def test_deployment_does_not_repeat_an_explained_failure(tmp_path: Path) -> None:
    failing_rdma = """<testsuite name="K8sEastWestNetworkRDMAPing-ethernet">
      <testcase name="probe"><failure message="unreachable"/></testcase></testsuite>"""
    result = _execute(
        tmp_path,
        '<testsuite name="network/validation"><testcase name="HelmValues"/></testsuite>' + failing_rdma,
        K8sNetworkOperatorDeployment,
        success=False,
    )
    assert result["passed"]


@pytest.mark.parametrize("check", [K8sEastWestNetworkRDMAPing, K8sNetworkOperatorDeployment, LaunchKitSosreport])
def test_unconfigured_inputs_skip_without_running_launch_kit(
    monkeypatch: pytest.MonkeyPatch, check: type[BaseValidation]
) -> None:
    def must_not_run(*_: object, **__: object) -> dict:
        raise AssertionError("Launch Kit must not run without inputs")

    monkeypatch.setattr(checks, "run_launch_kit", must_not_run)
    config = {"fabric": "ethernet", "user_config": "", "deployment_files": ""}
    with pytest.raises(pytest.skip.Exception, match="Launch Kit inputs not configured"):
        check(config=config).execute()


def test_sosreport_reports_collected_diagnostics(tmp_path: Path) -> None:
    result = _execute(tmp_path, _PASSING_RDMA, LaunchKitSosreport)
    assert result["passed"]
    assert result["output"] == "sosreport collected: /evidence/sosreport.tar.gz"


def test_failed_sosreport_fails_only_the_sosreport_check(tmp_path: Path) -> None:
    failed = {"success": False, "artifacts": {}, "error": "l8k sosreport exited with code 1"}
    assert _execute(tmp_path, _PASSING_RDMA, sosreport=failed)["passed"]
    result = _execute(tmp_path, _PASSING_RDMA, LaunchKitSosreport, sosreport=failed)
    assert not result["passed"]
    assert result["error"] == "sosreport collection failed: l8k sosreport exited with code 1"


@pytest.mark.parametrize("check", [K8sEastWestNetworkRDMAPing, K8sNetworkOperatorDeployment])
def test_run_error_leads_when_no_report(check: type[BaseValidation]) -> None:
    """A Launch Kit run that produced no report says why, not just that the report is missing."""
    checks.run_launch_kit().update({"success": False, "artifacts": {}, "error": "l8k validate exited with code 2"})

    result = check(config={"fabric": "ethernet", **_INPUTS}).execute()

    assert result["passed"] is False
    assert result["error"] == "l8k validate exited with code 2 (Missing Launch Kit JUnit artifact)"


@pytest.mark.parametrize("check", [K8sEastWestNetworkRDMAPing, K8sNetworkOperatorDeployment, LaunchKitSosreport])
def test_launch_kit_that_cannot_start_skips(check: type[BaseValidation]) -> None:
    """A missing prerequisite means the test never ran: skip, do not fail."""
    reason = "Launch Kit executable not found on PATH: l8k"
    checks.run_launch_kit().update(
        {"success": False, "artifacts": {}, "skip_reason": reason, "sosreport": {"skip_reason": reason}}
    )

    with pytest.raises(pytest.skip.Exception, match="Launch Kit executable not found on PATH: l8k"):
        check(config={"fabric": "ethernet", **_INPUTS}).execute()
