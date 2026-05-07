# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Simple subtest support for isvtest validations.

This module provides a lightweight subtest system for reporting nested test results
within validation tests. Designed specifically for reporting Go test results from
container-based validators.

Features:
- Consistent output formatting (PASSED/FAILED/SKIPPED, not SUBPASS)
- Proper newline handling
- Integration with pytest summary counts
- JUnit-compatible reporting (subtests appear as individual testcases)
"""

from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

import _pytest.terminal
import attr
import pytest
from _pytest._code import ExceptionInfo
from _pytest.reports import TestReport
from _pytest.runner import CallInfo

if TYPE_CHECKING:
    from _pytest.fixtures import SubRequest


@attr.s
class SubTestContext:
    """Context information for a subtest."""

    msg: str | None = attr.ib()
    kwargs: dict[str, Any] = attr.ib(factory=dict)


@attr.s(init=False)
class SubTestReport(TestReport):  # type: ignore[misc]
    """A test report for a subtest."""

    context: SubTestContext = attr.ib()

    @property
    def head_line(self) -> str:
        """Return the head line for this report."""
        _, _, domain = self.location
        return f"{domain} {self.sub_test_description()}"

    def sub_test_description(self) -> str:
        """Return a description of the subtest."""
        parts = []
        if isinstance(self.context.msg, str):
            parts.append(f"[{self.context.msg}]")
        if self.context.kwargs:
            params_desc = ", ".join(f"{k}={v!r}" for (k, v) in sorted(self.context.kwargs.items()))
            parts.append(f"({params_desc})")
        return " ".join(parts) or "(<subtest>)"

    @classmethod
    def _from_test_report(cls, test_report: TestReport) -> SubTestReport:
        """Create a SubTestReport from a TestReport.

        Note: We use cls._from_json() instead of super()._from_json() because
        super() would pass TestReport as cls to _from_json, creating a TestReport
        instance. Using cls ensures _from_json creates a SubTestReport instance.
        """
        return cls._from_json(test_report._to_json())


class SubTests:
    """Simple subtest fixture for reporting nested test results.

    Usage:
        def test_validation(subtests):
            with subtests.test(msg="TestGPU"):
                assert gpu_works()

            with subtests.test(msg="TestNetwork"):
                assert network_works()
    """

    def __init__(self, request: SubRequest) -> None:
        """Initialize the SubTests fixture.

        Args:
            request: pytest request fixture.
        """
        self._request = request
        self._first_subtest = True

        # Get capture manager for bypassing capture when printing
        capman = request.config.pluginmanager.get_plugin("capturemanager")
        self._suspend_capture = capman.global_and_fixture_disabled if capman else None

        # Get terminal writer for proper output integration
        terminalreporter = request.config.pluginmanager.get_plugin("terminalreporter")
        self._terminal_writer = terminalreporter._tw if terminalreporter else None

    @property
    def item(self) -> pytest.Item | pytest.Function:
        """Return the test item."""
        return self._request.node  # type: ignore[return-value]

    def test(
        self,
        msg: str | None = None,
        *,
        duration: float | None = None,
        skipped: bool = False,
        message: str | None = None,
        **kwargs: Any,
    ) -> _SubTestContextManager:
        """Context manager for a subtest.

        Args:
            msg: Message/name for the subtest.
            duration: Optional pre-measured duration in seconds. If provided,
                this duration is used instead of measuring the context block time.
                Useful for reporting results from tests that already ran (e.g., Go tests).
            skipped: If True, mark the subtest as skipped. This avoids using
                pytest.skip() which would add skip markers to the parent test.
            message: Optional human-readable reason. When ``skipped=True``, this
                is what surfaces in the report (e.g., "no CSIDriver objects
                present") instead of the canned "Subtest <name> skipped".
            **kwargs: Additional parameters for the subtest.

        Returns:
            Context manager that captures the subtest result.

        Example:
            with subtests.test(msg="TestGPUAccess"):
                result = run_gpu_test()
                assert result.success

            # With pre-measured duration (for reporting already-completed tests):
            with subtests.test(msg="TestFromGo", duration=1.5):
                if not go_test_passed:
                    raise AssertionError("Go test failed")

            # Mark as skipped without calling pytest.skip():
            with subtests.test(msg="TestOptional", skipped=True, message="not configured"):
                pass  # Nothing to run, just recording the skip
        """
        is_first = self._first_subtest
        self._first_subtest = False
        return _SubTestContextManager(
            request=self._request,
            msg=msg,
            kwargs=kwargs,
            is_first=is_first,
            suspend_capture=self._suspend_capture,
            terminal_writer=self._terminal_writer,
            provided_duration=duration,
            skipped=skipped,
            message=message,
        )


class _SubTestContextManager:
    """Context manager for a single subtest.

    Exception Suppression Behavior:
        By default, this context manager suppresses exceptions raised within its
        block (returns True from __exit__). This allows subsequent subtests to
        continue executing even when earlier subtests fail, enabling comprehensive
        test reporting where all subtests run regardless of individual failures.

        The exception is NOT suppressed (returns False from __exit__) when
        session.shouldfail is set, which occurs with pytest's --exitfirst/-x flag.
        In this case, the exception propagates and stops the parent test
        immediately.

    See Also:
        SubTests.test: The public API that creates instances of this context manager.
        __exit__: The method implementing the suppression logic via session.shouldfail.
    """

    def __init__(
        self,
        request: SubRequest,
        msg: str | None,
        kwargs: dict[str, Any],
        is_first: bool,
        suspend_capture: Any | None = None,
        terminal_writer: Any | None = None,
        provided_duration: float | None = None,
        skipped: bool = False,
        message: str | None = None,
    ) -> None:
        self._request = request
        self._msg = msg
        self._kwargs = kwargs
        self._is_first = is_first
        self._suspend_capture = suspend_capture
        self._terminal_writer = terminal_writer
        self._provided_duration = provided_duration
        self._skipped = skipped
        self._message = message
        self._start: float = 0
        self._precise_start: float = 0

    def __enter__(self) -> None:
        self._start = time.time()
        self._precise_start = time.perf_counter()

        # Print newline before first subtest to separate from test name
        if self._is_first:
            capture_ctx = self._suspend_capture() if self._suspend_capture else nullcontext()
            with capture_ctx:
                if self._terminal_writer:
                    self._terminal_writer.line()  # Newline via terminal writer
                else:
                    print()  # Fallback
                    sys.stdout.flush()

    def __exit__(
        self,
        exc_type: type[Exception] | None,
        exc_val: Exception | None,
        exc_tb: Any,
    ) -> bool:
        __tracebackhide__ = True

        # Calculate duration (use provided duration if available)
        precise_stop = time.perf_counter()
        if self._provided_duration is not None:
            duration = self._provided_duration
        else:
            duration = precise_stop - self._precise_start
        stop = time.time()

        # Create exception info if there was an exception
        if exc_val is not None:
            exc_info = ExceptionInfo.from_exception(exc_val)
        else:
            exc_info = None

        # Create call info
        call_info = CallInfo(
            None,
            exc_info,
            start=self._start,
            stop=stop,
            duration=duration,
            when="call",
            _ispytest=True,
        )

        # Create report through pytest hooks
        ihook = self._request.node.ihook
        report = ihook.pytest_runtest_makereport(item=self._request.node, call=call_info)
        sub_report = SubTestReport._from_test_report(report)
        sub_report.context = SubTestContext(self._msg, self._kwargs.copy())

        # Handle skipped flag: modify the report outcome directly
        # This avoids calling pytest.skip() which would add markers to the parent test
        if self._skipped and exc_val is None:
            sub_report.outcome = "skipped"
            sub_report.longrepr = (
                __file__,
                0,
                self._message or f"Subtest {self._msg} skipped",
            )

        # Print the subtest result ourselves for consistent formatting
        self._print_result(sub_report)

        # For subtests marked as skipped via the skipped parameter, collect directly
        # without calling pytest_runtest_logreport to avoid pytest's JUnit plugin
        # adding skip markers to the parent test
        if self._skipped and exc_val is None:
            _collected_subtest_reports.append(sub_report)
        else:
            # Report to pytest for counting purposes
            # (our hook returns empty strings to suppress default terminal output)
            ihook.pytest_runtest_logreport(report=sub_report)

        # Suppress the exception - subtests continue even on failure
        if exc_val is not None:
            # Check if we should stop on first failure
            if self._request.session.shouldfail:
                return False
        return True

    def _print_result(self, report: SubTestReport) -> None:
        """Print the subtest result with consistent formatting.

        Uses pytest's terminal writer if available for better integration,
        otherwise falls back to print with capture suspension.
        """
        description = report.sub_test_description()

        if report.passed:
            status = "PASSED"
            markup = {"green": True}
        elif report.skipped:
            status = "SKIPPED"
            markup = {"yellow": True}
        else:
            status = "FAILED"
            markup = {"red": True}

        line = f"  SUBTEST {description} {status}"

        # Use capture manager context if available, otherwise nullcontext
        capture_ctx = self._suspend_capture() if self._suspend_capture else nullcontext()

        with capture_ctx:
            if self._terminal_writer:
                # Use pytest's terminal writer for proper integration
                self._terminal_writer.line(line, **markup)
            else:
                # Fallback to print with ANSI colors
                if report.passed:
                    color = "\033[32m"  # Green
                elif report.skipped:
                    color = "\033[33m"  # Yellow
                else:
                    color = "\033[31m"  # Red
                reset = "\033[0m"

                use_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
                if use_color:
                    print(f"{color}{line}{reset}")
                else:
                    print(line)

                sys.stdout.flush()


def pytest_configure(config: pytest.Config) -> None:
    """Register the subtests status types with pytest terminal and initialize collection."""
    # Add our custom status types (with defensive guards for pytest internal API changes)
    new_types = ("subtest passed", "subtest failed", "subtest skipped")

    known_types = getattr(_pytest.terminal, "KNOWN_TYPES", None)
    color_map = getattr(_pytest.terminal, "_color_for_type", None)

    # Only add if not already present and KNOWN_TYPES exists
    if isinstance(known_types, tuple) and new_types[0] not in known_types:
        _pytest.terminal.KNOWN_TYPES = known_types + new_types  # type: ignore[assignment]

    # Add colors for our status types if color map exists
    if isinstance(color_map, dict):
        color_map.update(
            {
                "subtest passed": color_map.get("passed", "green"),
                "subtest failed": color_map.get("failed", "red"),
                "subtest skipped": color_map.get("skipped", "yellow"),
            }
        )


@pytest.hookimpl(tryfirst=True)
def pytest_report_teststatus(
    report: pytest.TestReport,
    config: pytest.Config,
) -> tuple[str, str, str] | None:
    """Custom test status reporting for subtests.

    This hook controls how subtests appear in pytest output.
    We print the results ourselves for consistent formatting, so we return
    empty strings for the verbose output to avoid duplicate printing.
    """
    if report.when != "call" or not isinstance(report, SubTestReport):
        return None

    # We handle printing ourselves, so return empty verbose message
    # but still categorize for the summary counts
    if report.passed:
        return "subtest passed", "", ""
    elif report.skipped:
        return "subtest skipped", "", ""
    elif report.failed:
        return "subtest failed", "", ""

    return None


@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Collect SubTestReports for JUnit integration.

    This hook runs after each test report is logged. We collect SubTestReports
    so they can be added to the JUnit XML at session end.
    """
    if isinstance(report, SubTestReport) and report.when == "call":
        # Access config through the report's fspath (stored during report creation)
        # We use a module-level collector since we can't access config here
        _collected_subtest_reports.append(report)


# Module-level collector for SubTestReports (fallback when stash isn't accessible)
_collected_subtest_reports: list[SubTestReport] = []


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Add collected subtests to JUnit XML after pytest finishes writing it.

    This post-processes the JUnit XML file to add SubTestReports as individual
    testcase elements, making them visible to CI systems and test reporters.
    """
    global _collected_subtest_reports

    config = session.config

    # Get the JUnit XML path from pytest config
    junit_xml_path = config.option.xmlpath
    if not junit_xml_path:
        return  # No JUnit output configured

    junit_path = Path(junit_xml_path)
    if not junit_path.exists():
        return  # JUnit file not created yet (shouldn't happen)

    _normalize_junit_testcase_names(junit_path)

    if _collected_subtest_reports:
        _inject_subtests_into_junit(junit_path, _collected_subtest_reports)
        _collected_subtest_reports = []


_PARAMETRIZED_NAME_RE = re.compile(r"^\w+\[(.+)]$")


def _normalize_junit_testcase_names(junit_path: Path) -> None:
    """Strip pytest parametrize wrappers from testcase names in JUnit XML.

    Transforms names like ``test_validation[K8sNodeCountCheck]`` to
    ``K8sNodeCountCheck`` so they match the test catalog used by the backend.
    """
    try:
        tree = ET.parse(junit_path)
        changed = False
        for testcase in tree.iter("testcase"):
            name = testcase.get("name", "")
            m = _PARAMETRIZED_NAME_RE.match(name)
            if m:
                testcase.set("name", m.group(1))
                changed = True
        if changed:
            tree.write(junit_path, encoding="utf-8", xml_declaration=True)
    except Exception:
        pass  # don't break the test run for post-processing failures


def _inject_subtests_into_junit(junit_path: Path, reports: list[SubTestReport]) -> None:
    """Inject SubTestReports as testcase elements into the JUnit XML.

    Subtests are inserted immediately after their parent testcase element
    to maintain logical grouping in the XML output.

    Args:
        junit_path: Path to the JUnit XML file.
        reports: List of SubTestReport instances to add.
    """
    try:
        tree = ET.parse(junit_path)
        root = tree.getroot()

        # Find testsuite(s)
        if root.tag == "testsuites":
            testsuites = list(root.iter("testsuite"))
        elif root.tag == "testsuite":
            testsuites = [root]
        else:
            return  # Unsupported format

        if not testsuites:
            return

        # Use the first testsuite (pytest typically creates one)
        testsuite = testsuites[0]

        # Group subtests by parent nodeid
        subtests_by_parent: dict[str, list[SubTestReport]] = {}
        for report in reports:
            parent_nodeid = report.nodeid
            if parent_nodeid not in subtests_by_parent:
                subtests_by_parent[parent_nodeid] = []
            subtests_by_parent[parent_nodeid].append(report)

        # Build a map of testcase name -> index in testsuite
        testcases = list(testsuite)
        testcase_indices: dict[str, int] = {}
        for idx, tc in enumerate(testcases):
            name = tc.get("name", "")
            testcase_indices[name] = idx

        # Track counts for updating testsuite attributes
        added_tests = 0
        added_failures = 0
        added_skipped = 0

        # Insert subtests after their parent, in reverse order of parent index
        # to avoid index shifting issues
        insertions: list[tuple[int, list[ET.Element]]] = []

        for parent_nodeid, parent_reports in subtests_by_parent.items():
            raw_parent = parent_nodeid.split("::")[-1] if "::" in parent_nodeid else parent_nodeid
            m = _PARAMETRIZED_NAME_RE.match(raw_parent)
            parent_name = m.group(1) if m else raw_parent
            parent_idx = testcase_indices.get(parent_name)

            if parent_idx is None:
                # Try to find by partial match (name contains the test name)
                for tc_name, idx in testcase_indices.items():
                    if parent_name in tc_name or tc_name in parent_name:
                        parent_idx = idx
                        break

            subtest_elements: list[ET.Element] = []
            for report in parent_reports:
                # Create testcase element
                testcase = ET.Element("testcase")
                subtest_desc = report.context.msg or "<subtest>"
                testcase.set("name", f"{parent_name}::{subtest_desc}")
                testcase.set("classname", "")  # Match pytest's default
                testcase.set("time", f"{report.duration:.3f}")

                added_tests += 1

                if report.failed:
                    added_failures += 1
                    failure = ET.SubElement(testcase, "failure")
                    failure.set("message", f"Subtest {subtest_desc} failed")
                    if report.longrepr:
                        failure.text = str(report.longrepr)[:2000]  # Limit length
                elif report.skipped:
                    added_skipped += 1
                    skipped_elem = ET.SubElement(testcase, "skipped")
                    # longrepr from both our skipped-flag path and pytest.skip()
                    # is a (file, line, msg) tuple; surface the msg so the
                    # report carries the actual reason instead of a canned line.
                    if isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
                        skip_text = str(report.longrepr[2])
                    else:
                        skip_text = f"Subtest {subtest_desc} skipped"
                    skipped_elem.set("message", skip_text)

                subtest_elements.append(testcase)

            if parent_idx is not None:
                insertions.append((parent_idx, subtest_elements))
            else:
                # Fallback: append to end if parent not found
                for elem in subtest_elements:
                    testsuite.append(elem)

        # Sort insertions by index descending to avoid shifting issues
        insertions.sort(key=lambda x: x[0], reverse=True)

        for parent_idx, subtest_elements in insertions:
            # Insert after parent (at parent_idx + 1)
            insert_pos = parent_idx + 1
            for i, elem in enumerate(subtest_elements):
                testsuite.insert(insert_pos + i, elem)

        # Update testsuite counts
        current_tests = int(testsuite.get("tests", "0"))
        current_failures = int(testsuite.get("failures", "0"))
        current_skipped = int(testsuite.get("skipped", "0"))

        testsuite.set("tests", str(current_tests + added_tests))
        testsuite.set("failures", str(current_failures + added_failures))
        testsuite.set("skipped", str(current_skipped + added_skipped))

        # Write back
        tree.write(junit_path, encoding="utf-8", xml_declaration=True)

    except Exception:
        # Don't fail the test run if JUnit post-processing fails;
        # log a warning for visibility and skip injection.
        import warnings

        warnings.warn(
            "Failed to inject subtests into JUnit XML; subtest results may be missing from reports.",
            RuntimeWarning,
            stacklevel=2,
        )


@pytest.fixture
def subtests(request: SubRequest) -> SubTests:
    """Fixture providing simple subtest support.

    Args:
        request: pytest request fixture.

    Returns:
        SubTests instance for creating subtests.

    Example:
        def test_validation(subtests):
            with subtests.test(msg="check_gpu"):
                assert has_gpu()

            with subtests.test(msg="check_memory"):
                assert has_memory()
    """
    return SubTests(request)
