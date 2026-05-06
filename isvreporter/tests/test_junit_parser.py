# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for JUnit XML parser."""

from pathlib import Path

import pytest

from isvreporter.junit_parser import JUnitReport, TestResult, TestSuite, parse_junit_xml


@pytest.fixture
def sample_junit_xml() -> str:
    """Sample JUnit XML for testing."""
    return """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="1" failures="2" skipped="1" tests="5" time="1.234"
             timestamp="2024-01-01T12:00:00Z" hostname="test-host-01">
    <testcase classname="test_module.TestClass" name="test_passing" time="0.100">
    </testcase>
    <testcase classname="test_module.TestClass" name="test_failing" time="0.200">
      <failure message="assert False">
def test_failing():
&gt;   assert False
E   assert False

test_module.py:10: AssertionError
      </failure>
    </testcase>
    <testcase classname="test_module.TestClass" name="test_error" time="0.300">
      <error message="ImportError: No module named 'missing'" type="ImportError">
Traceback (most recent call last):
  File "test_module.py", line 5, in test_error
    import missing
ImportError: No module named 'missing'
      </error>
    </testcase>
    <testcase classname="test_module.TestClass" name="test_skipped" time="0.001">
      <skipped message="Test skipped" type="pytest.skip">
Test is not ready yet
      </skipped>
    </testcase>
    <testcase classname="test_module.TestClass" name="test_with_output" time="0.500">
      <system-out>Standard output text</system-out>
      <system-err>Standard error text</system-err>
    </testcase>
  </testsuite>
</testsuites>"""


@pytest.fixture
def junit_xml_file(sample_junit_xml: str, tmp_path: Path) -> Path:
    """Create a temporary JUnit XML file."""
    path = tmp_path / "report.xml"
    path.write_text(sample_junit_xml)
    return path


def test_parse_junit_xml_basic(junit_xml_file: Path) -> None:
    """Test basic JUnit XML parsing."""
    report = parse_junit_xml(junit_xml_file)

    assert isinstance(report, JUnitReport)
    assert report.total_tests == 5
    assert report.total_failures == 2
    assert report.total_errors == 1
    assert report.total_skipped == 1
    assert report.total_duration_seconds == pytest.approx(1.234)


def test_parse_junit_xml_suites(junit_xml_file: Path) -> None:
    """Test test suite parsing."""
    report = parse_junit_xml(junit_xml_file)

    assert len(report.suites) == 1
    suite = report.suites[0]

    assert suite.name == "pytest"
    assert suite.tests == 5
    assert suite.failures == 2
    assert suite.errors == 1
    assert suite.skipped == 1
    assert suite.duration_seconds == pytest.approx(1.234)
    assert suite.timestamp == "2024-01-01T12:00:00Z"
    assert suite.hostname == "test-host-01"


def test_parse_junit_xml_results(junit_xml_file: Path) -> None:
    """Test individual test result parsing."""
    report = parse_junit_xml(junit_xml_file)

    assert len(report.results) == 5

    # Test passing test
    passing = next(r for r in report.results if r.name == "test_passing")
    assert passing.status == "PASSED"
    assert passing.classname == "test_module.TestClass"
    assert passing.duration_seconds == pytest.approx(0.1)
    assert passing.error_message is None

    # Test failing test
    failing = next(r for r in report.results if r.name == "test_failing")
    assert failing.status == "FAILED"
    assert failing.error_message == "assert False"
    assert "AssertionError" in failing.error_details

    # Test error test
    error = next(r for r in report.results if r.name == "test_error")
    assert error.status == "ERROR"
    assert "ImportError" in error.error_message
    assert error.error_type == "ImportError"

    # Test skipped test
    skipped = next(r for r in report.results if r.name == "test_skipped")
    assert skipped.status == "SKIPPED"
    assert skipped.error_message == "Test skipped"

    # Test with output
    with_output = next(r for r in report.results if r.name == "test_with_output")
    assert with_output.status == "PASSED"
    assert with_output.system_out == "Standard output text"
    assert with_output.system_err == "Standard error text"


def test_parse_junit_xml_file_not_found() -> None:
    """Test parsing non-existent file."""
    with pytest.raises(FileNotFoundError):
        parse_junit_xml("nonexistent.xml")


def test_test_result_to_dict() -> None:
    """Test TestResult to_dict conversion."""
    result = TestResult(
        name="test_example",
        classname="test_module.TestClass",
        duration_seconds=1.234,
        status="FAILED",
        error_message="Test failed",
        error_type="AssertionError",
        error_details="Full traceback",
        system_out="stdout",
        system_err="stderr",
    )

    result_dict = result.to_dict()

    assert result_dict["testName"] == "test_example"
    assert result_dict["testClass"] == "test_module.TestClass"
    assert result_dict["testDurationInSeconds"] == pytest.approx(1.234)
    assert result_dict["testStatus"] == "FAILED"
    assert result_dict["errorMessage"] == "Test failed"
    assert result_dict["errorType"] == "AssertionError"
    assert result_dict["errorDetails"] == "Full traceback"
    assert result_dict["systemOut"] == "stdout"
    assert result_dict["systemErr"] == "stderr"


def test_test_suite_to_dict() -> None:
    """Test TestSuite to_dict conversion."""
    suite = TestSuite(
        name="pytest",
        tests=10,
        failures=2,
        errors=1,
        skipped=1,
        duration_seconds=5.678,
        timestamp="2024-01-01T12:00:00Z",
        hostname="test-host",
    )

    suite_dict = suite.to_dict()

    assert suite_dict["suiteName"] == "pytest"
    assert suite_dict["totalTests"] == 10
    assert suite_dict["failedTests"] == 2
    assert suite_dict["errorTests"] == 1
    assert suite_dict["skippedTests"] == 1
    assert suite_dict["suiteDurationInSeconds"] == pytest.approx(5.678)
    assert suite_dict["timestamp"] == "2024-01-01T12:00:00Z"
    assert suite_dict["hostname"] == "test-host"


def test_junit_report_to_dict(junit_xml_file: Path) -> None:
    """Test JUnitReport to_dict conversion."""
    report = parse_junit_xml(junit_xml_file)
    report_dict = report.to_dict()

    assert "summary" in report_dict
    assert "testSuites" in report_dict
    assert "testResults" in report_dict

    summary = report_dict["summary"]
    assert summary["totalTests"] == 5
    assert summary["totalFailures"] == 2
    assert summary["totalErrors"] == 1
    assert summary["totalSkipped"] == 1
    assert summary["totalPassed"] == 1  # 5 - 2 - 1 - 1
    assert summary["totalDurationInSeconds"] == pytest.approx(1.234)

    assert len(report_dict["testSuites"]) == 1
    assert len(report_dict["testResults"]) == 5


def test_parse_single_testsuite(tmp_path: Path) -> None:
    """Test parsing XML with single testsuite (no testsuites wrapper)."""
    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="single-suite" errors="0" failures="1" skipped="0" tests="2" time="0.5">
  <testcase classname="test_module" name="test_pass" time="0.3"></testcase>
  <testcase classname="test_module" name="test_fail" time="0.2">
    <failure message="failed">Error details</failure>
  </testcase>
</testsuite>"""

    xml_file = tmp_path / "single.xml"
    xml_file.write_text(xml_content)

    report = parse_junit_xml(xml_file)

    assert report.total_tests == 2
    assert report.total_failures == 1
    assert len(report.results) == 2
    assert len(report.suites) == 1


def test_empty_junit_xml(tmp_path: Path) -> None:
    """Test parsing empty test suite."""
    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="empty" errors="0" failures="0" skipped="0" tests="0" time="0"></testsuite>
</testsuites>"""

    xml_file = tmp_path / "empty.xml"
    xml_file.write_text(xml_content)

    report = parse_junit_xml(xml_file)

    assert report.total_tests == 0
    assert report.total_failures == 0
    assert report.total_errors == 0
    assert report.total_skipped == 0
    assert len(report.results) == 0
