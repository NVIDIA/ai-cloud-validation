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

"""JUnit XML parser for extracting test results."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TestResult:
    """Individual test case result."""

    __test__ = False  # Prevent pytest from collecting this class

    name: str
    classname: str
    duration_seconds: float
    status: str  # PASSED, FAILED, SKIPPED, ERROR
    error_message: str | None = None
    error_type: str | None = None
    error_details: str | None = None
    system_out: str | None = None
    system_err: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API payload."""
        result: dict[str, Any] = {
            "testName": self.name,
            "testClass": self.classname,
            "testDurationInSeconds": round(self.duration_seconds, 3),
            "testStatus": self.status,
        }

        if self.error_message:
            result["errorMessage"] = self.error_message
        if self.error_type:
            result["errorType"] = self.error_type
        if self.error_details:
            result["errorDetails"] = self.error_details
        if self.system_out:
            result["systemOut"] = self.system_out
        if self.system_err:
            result["systemErr"] = self.system_err

        return result


@dataclass
class TestSuite:
    """Test suite summary."""

    __test__ = False  # Prevent pytest from collecting this class

    name: str
    tests: int
    failures: int
    errors: int
    skipped: int
    duration_seconds: float
    timestamp: str | None = None
    hostname: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API payload."""
        result: dict[str, Any] = {
            "suiteName": self.name,
            "totalTests": self.tests,
            "failedTests": self.failures,
            "errorTests": self.errors,
            "skippedTests": self.skipped,
            "suiteDurationInSeconds": round(self.duration_seconds, 3),
        }

        if self.timestamp:
            result["timestamp"] = self.timestamp
        if self.hostname:
            result["hostname"] = self.hostname

        return result


@dataclass
class JUnitReport:
    """Complete JUnit test report."""

    suites: list[TestSuite]
    results: list[TestResult]
    total_tests: int
    total_failures: int
    total_errors: int
    total_skipped: int
    total_duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API payload."""
        return {
            "summary": {
                "totalTests": self.total_tests,
                "totalFailures": self.total_failures,
                "totalErrors": self.total_errors,
                "totalSkipped": self.total_skipped,
                "totalPassed": self.total_tests - self.total_failures - self.total_errors - self.total_skipped,
                "totalDurationInSeconds": round(self.total_duration_seconds, 3),
            },
            "testSuites": [suite.to_dict() for suite in self.suites],
            "testResults": [result.to_dict() for result in self.results],
        }


def parse_junit_xml(xml_path: str | Path) -> JUnitReport:
    """
    Parse JUnit XML file and extract test results.

    Args:
        xml_path: Path to JUnit XML file

    Returns:
        JUnitReport containing all test results

    Raises:
        FileNotFoundError: If XML file doesn't exist
        ET.ParseError: If XML is malformed
    """
    xml_file = Path(xml_path)
    if not xml_file.exists():
        raise FileNotFoundError(f"JUnit XML file not found: {xml_file}")

    tree = ET.parse(xml_file)
    root = tree.getroot()

    suites: list[TestSuite] = []
    results: list[TestResult] = []
    total_tests = 0
    total_failures = 0
    total_errors = 0
    total_skipped = 0
    total_duration = 0.0

    # Handle both single testsuite and testsuites wrapper
    testsuites = root.findall(".//testsuite")
    if not testsuites:
        # Root might be a testsuite itself
        if root.tag == "testsuite":
            testsuites = [root]

    for suite_elem in testsuites:
        # Parse suite metadata
        suite_name = suite_elem.get("name", "unknown")
        suite_tests = int(suite_elem.get("tests", "0"))
        suite_failures = int(suite_elem.get("failures", "0"))
        suite_errors = int(suite_elem.get("errors", "0"))
        suite_skipped = int(suite_elem.get("skipped", "0"))
        suite_time = float(suite_elem.get("time", "0"))
        suite_timestamp = suite_elem.get("timestamp")
        suite_hostname = suite_elem.get("hostname")

        suite = TestSuite(
            name=suite_name,
            tests=suite_tests,
            failures=suite_failures,
            errors=suite_errors,
            skipped=suite_skipped,
            duration_seconds=suite_time,
            timestamp=suite_timestamp,
            hostname=suite_hostname,
        )
        suites.append(suite)

        total_tests += suite_tests
        total_failures += suite_failures
        total_errors += suite_errors
        total_skipped += suite_skipped
        total_duration += suite_time

        # Parse individual test cases
        for testcase in suite_elem.findall("testcase"):
            name = testcase.get("name", "unknown")
            classname = testcase.get("classname", "unknown")
            time = float(testcase.get("time", "0"))

            # Determine test status and extract error info
            status = "PASSED"
            error_message = None
            error_type = None
            error_details = None

            failure = testcase.find("failure")
            if failure is not None:
                status = "FAILED"
                error_message = failure.get("message")
                error_type = failure.get("type", "AssertionError")
                error_details = failure.text

            error = testcase.find("error")
            if error is not None:
                status = "ERROR"
                error_message = error.get("message")
                error_type = error.get("type", "Error")
                error_details = error.text

            skipped = testcase.find("skipped")
            if skipped is not None:
                status = "SKIPPED"
                error_message = skipped.get("message")
                error_type = skipped.get("type")
                error_details = skipped.text

            # Extract system output
            system_out = testcase.find("system-out")
            system_out_text = system_out.text if system_out is not None else None

            system_err = testcase.find("system-err")
            system_err_text = system_err.text if system_err is not None else None

            result = TestResult(
                name=name,
                classname=classname,
                duration_seconds=time,
                status=status,
                error_message=error_message,
                error_type=error_type,
                error_details=error_details,
                system_out=system_out_text,
                system_err=system_err_text,
            )
            results.append(result)

    return JUnitReport(
        suites=suites,
        results=results,
        total_tests=total_tests,
        total_failures=total_failures,
        total_errors=total_errors,
        total_skipped=total_skipped,
        total_duration_seconds=total_duration,
    )
