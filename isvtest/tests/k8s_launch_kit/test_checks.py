# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native JUnit import tests."""

from pathlib import Path

import pytest

from isvtest.validations.k8s_launch_kit.checks import LaunchKitConnectivityCheck

pytestmark = pytest.mark.unit


def _execute(tmp_path: Path, cases: str, *, success: bool = True) -> dict:
    report = tmp_path / "junit.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    return LaunchKitConnectivityCheck(
        config={
            "step_output": {
                "operation": "validate",
                "success": success,
                "error": "process failed",
                "artifacts": {"validation_junit": str(report)},
            }
        }
    ).execute()


def test_native_names_status_duration_and_evidence(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        """
      <testcase name="K8sEastWestNetworkRDMAPing-ethernet::a→b" classname="network.connectivity" time="0.125">
        <system-out>srcGPU=2 bandwidthGbps=187.6</system-out>
      </testcase>
      <testcase name="K8sEastWestNetworkRDMAPing-infiniband" classname="network.connectivity">
        <skipped message="Cluster fabric is not configured for this fabric type: infiniband"/>
      </testcase>""",
    )
    assert result["passed"]
    passed, skipped = result["subtests"]
    assert passed["name"] == "K8sEastWestNetworkRDMAPing-ethernet::a→b"
    assert passed["duration"] == 0.125
    assert "bandwidthGbps=187.6" in passed["message"]
    assert skipped["skipped"]
    assert "not configured" in skipped["message"]


@pytest.mark.parametrize("tag", ["failure", "error"])
def test_native_failures_keep_evidence(tmp_path: Path, tag: str) -> None:
    result = _execute(
        tmp_path,
        f"""<testcase name="probe" classname="network.connectivity">
      <{tag} message="bandwidth below threshold">42.5 &lt; 100</{tag}>
      <system-err>connection refused</system-err></testcase>""",
    )
    assert not result["passed"]
    assert "42.5 < 100" in result["error"]
    assert "connection refused" in result["error"]
    assert not result["subtests"][0]["passed"]


@pytest.mark.parametrize(
    "cases",
    [
        "",
        '<testcase name="disabled" classname="network.connectivity"><skipped/></testcase>',
        '<testcase name="static"/>',
    ],
)
def test_no_executed_connectivity_fails(tmp_path: Path, cases: str) -> None:
    assert not _execute(tmp_path, cases)["passed"]


def test_process_failure_cannot_be_hidden_by_passing_xml(tmp_path: Path) -> None:
    result = _execute(tmp_path, '<testcase name="probe" classname="network.connectivity"/>', success=False)
    assert not result["passed"]
    assert result["subtests"][0]["passed"]
    assert result["error"] == "process failed"


@pytest.mark.parametrize("contents", [None, "<broken", "<testcase time='invalid'/>"])
def test_missing_or_malformed_xml_fails(tmp_path: Path, contents: str | None) -> None:
    report = tmp_path / "junit.xml"
    if contents is not None:
        report.write_text(contents)
    result = LaunchKitConnectivityCheck(
        config={
            "step_output": {
                "operation": "validate",
                "success": True,
                "artifacts": {"validation_junit": str(report)},
            }
        }
    ).execute()
    assert not result["passed"]


@pytest.mark.parametrize("duration", ["NaN", "-1", "Infinity", "invalid"])
def test_invalid_duration_fails(tmp_path: Path, duration: str) -> None:
    result = _execute(tmp_path, f'<testcase name="probe" classname="network.connectivity" time="{duration}"/>')
    assert not result["passed"]


def test_static_error_is_preserved_alongside_connectivity(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        """
      <testcase name="future-family::probe" classname="network.connectivity"/>
      <testcase name="ValidationExecution" classname="network.validation"><error message="setup failed"/></testcase>
      <testcase name="K8sEastWestNetworkDMABufBandwidth-ethernet" classname="network.connectivity">
        <skipped message="Check disabled by validation configuration"/>
      </testcase>""",
    )
    assert not result["passed"]
    assert result["subtests"][0]["name"] == "future-family::probe"
    assert "setup failed" in result["error"]
    assert result["subtests"][2]["skipped"]
