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

"""JUnit XML post-processing to expand subtests into individual test cases.

This module provides utilities to parse validation results with subtests
and expand them into proper JUnit test cases for CI systems.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


def expand_subtests_in_junit(
    junit_path: str | Path,
    output_path: str | Path | None = None,
    parent_test_pattern: str = r".*Workload.*",
) -> Path:
    """Expand subtests from validation results into individual JUnit test cases.

    This post-processes a JUnit XML file to add individual test cases for
    subtests that were reported during validation runs. It parses the test
    output to find subtest results and adds them as separate testcase elements.

    Args:
        junit_path: Path to the input JUnit XML file.
        output_path: Path for the output file. If None, overwrites the input file.
        parent_test_pattern: Regex pattern to identify parent tests to expand.

    Returns:
        Path to the output JUnit XML file.

    Example:
        >>> expand_subtests_in_junit("junit-validation.xml")
        PosixPath('junit-validation.xml')

        # Or save to a new file:
        >>> expand_subtests_in_junit("junit.xml", "junit-expanded.xml")
        PosixPath('junit-expanded.xml')
    """
    junit_path = Path(junit_path)
    output_path = Path(output_path) if output_path else junit_path

    tree = ET.parse(junit_path)
    root = tree.getroot()

    # Find testsuites or testsuite element
    if root.tag == "testsuites":
        testsuites = list(root.iter("testsuite"))
    elif root.tag == "testsuite":
        testsuites = [root]
    else:
        # Unsupported format
        return output_path

    for testsuite in testsuites:
        testcases = list(testsuite.findall("testcase"))

        for testcase in testcases:
            name = testcase.get("name", "")

            # Check if this is a parent test that might have subtests
            # Using re.search for more intuitive pattern matching (matches anywhere in name)
            if not re.search(parent_test_pattern, name):
                continue

            # Get system-out which may contain subtest info
            system_out = testcase.find("system-out")
            if system_out is not None and system_out.text:
                _add_subtests_from_output(testsuite, testcase, system_out.text)

    # Update test count
    for testsuite in testsuites:
        test_count = len(testsuite.findall("testcase"))
        testsuite.set("tests", str(test_count))

    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path


def _add_subtests_from_output(
    testsuite: ET.Element,
    parent_testcase: ET.Element,
    output: str,
) -> None:
    """Parse test output and add subtest elements to the testsuite.

    Args:
        testsuite: Parent testsuite element to add testcases to.
        parent_testcase: Parent testcase element (for classname inheritance).
        output: Test output text containing Go test results.
    """
    parent_name = parent_testcase.get("name", "")
    parent_classname = parent_testcase.get("classname", "")

    # Parse Go test output: "--- PASS: TestName (1.23s)"
    pattern = r"---\s+(PASS|FAIL|SKIP):\s+(\S+)\s+\(([^)]+)\)"

    for match in re.finditer(pattern, output):
        status, test_name, duration_str = match.groups()

        # Skip nested subtests (those with "/" in name) for cleaner output
        # Only report top-level tests
        if "/" in test_name:
            continue

        # Parse duration
        try:
            duration = float(duration_str.rstrip("s"))
        except ValueError:
            duration = 0.0

        # Create new testcase element
        subtest = ET.SubElement(testsuite, "testcase")
        subtest.set("classname", parent_classname or "workload")
        subtest.set("name", f"{parent_name}::{test_name}")
        subtest.set("time", f"{duration:.3f}")

        if status == "FAIL":
            failure = ET.SubElement(subtest, "failure")
            failure.set("message", f"Go test {test_name} failed")
            # Try to extract failure details
            fail_pattern = rf"===\s+RUN\s+{re.escape(test_name)}\n(.*?)---\s+FAIL:\s+{re.escape(test_name)}"
            fail_match = re.search(fail_pattern, output, re.DOTALL)
            if fail_match:
                failure.text = fail_match.group(1).strip()[:1000]  # Limit length
        elif status == "SKIP":
            skipped = ET.SubElement(subtest, "skipped")
            # Try to find skip reason
            skip_pattern = rf"{re.escape(test_name)}.*?:\s*(.+?)(?:\n|$)"
            skip_match = re.search(skip_pattern, output)
            if skip_match:
                skipped.set("message", skip_match.group(1).strip())


def create_subtests_junit(
    subtests: list[dict],
    parent_name: str,
    output_path: str | Path,
    classname: str = "workload",
) -> Path:
    """Create a new JUnit XML file from a list of subtest results.

    This can be used to create a separate JUnit file containing only
    the subtests, which can then be merged with other test results.

    Args:
        subtests: List of subtest result dictionaries with keys:
            - name: Test name
            - passed: True if passed
            - skipped: True if skipped (optional)
            - duration: Duration in seconds (optional)
            - message: Result message (optional)
        parent_name: Name of the parent test (used as prefix).
        output_path: Path for the output JUnit XML file.
        classname: Class name for the test cases.

    Returns:
        Path to the created JUnit XML file.

    Example:
        >>> subtests = [
        ...     {"name": "TestGPU", "passed": True, "duration": 1.5},
        ...     {"name": "TestNCCL", "passed": False, "message": "timeout"},
        ... ]
        >>> create_subtests_junit(subtests, "K8sValidator", "subtests.xml")
    """
    output_path = Path(output_path)

    # Create JUnit structure
    testsuites = ET.Element("testsuites")
    testsuites.set("name", "subtests")

    testsuite = ET.SubElement(testsuites, "testsuite")
    testsuite.set("name", parent_name)

    passed = 0
    failed = 0
    skipped = 0
    total_time = 0.0

    for subtest in subtests:
        testcase = ET.SubElement(testsuite, "testcase")
        testcase.set("classname", classname)
        testcase.set("name", f"{parent_name}::{subtest['name']}")

        duration = subtest.get("duration", 0.0) or 0.0
        testcase.set("time", f"{duration:.3f}")
        total_time += duration

        if subtest.get("skipped"):
            skipped += 1
            skip_elem = ET.SubElement(testcase, "skipped")
            if subtest.get("message"):
                skip_elem.set("message", subtest["message"])
        elif not subtest.get("passed"):
            failed += 1
            failure = ET.SubElement(testcase, "failure")
            failure.set("message", subtest.get("message", "Test failed"))
        else:
            passed += 1

    testsuite.set("tests", str(len(subtests)))
    testsuite.set("failures", str(failed))
    testsuite.set("errors", "0")
    testsuite.set("skipped", str(skipped))
    testsuite.set("time", f"{total_time:.3f}")

    tree = ET.ElementTree(testsuites)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)

    return output_path
