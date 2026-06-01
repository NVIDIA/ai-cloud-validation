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

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from isvtest.core.logger import setup_logger
from isvtest.core.runners import CommandResult, LocalRunner, Runner

if TYPE_CHECKING:
    from isvtest.testing.subtests import SubTests

_logger = logging.getLogger(__name__)

# Sentinel for ``test_ids`` marking a validation that intentionally maps to no
# test-plan entry (a generic/reusable check, or one the plan has no item for).
# An empty ``test_ids`` means "not yet linked" and fails the coverage
# completeness guardrail; declare ``(UNMAPPED,)`` to record an intentional gap.
UNMAPPED: str = "N/A"

# Cache of discovered validation classes
_validation_class_cache: dict[str, type[BaseValidation]] | None = None


def check_required_tests(
    validation: BaseValidation,
    required_keys: list[str],
    fail_label: str,
) -> bool:
    """Check that step_output.tests contains every required key with passed=True.

    Sets failed on the validation if tests are missing or any required key did
    not pass. On success, returns True without setting passed - the caller is
    expected to call ``set_passed`` with a context-appropriate message.
    """
    step_output = validation.config.get("step_output", {})
    tests = step_output.get("tests", {})
    if not tests:
        validation.set_failed("No 'tests' in step output")
        return False

    failed = []
    for test_name in required_keys:
        test_result = tests.get(test_name, {})
        if not test_result.get("passed"):
            error = test_result.get("error", "test not found")
            failed.append(f"{test_name}: {error}")

    if failed:
        validation.set_failed(f"{fail_label}: {'; '.join(failed)}")
        return False
    return True


class BaseValidation(ABC):
    """Base class for all ISV validation tests."""

    # Optional metadata
    description: ClassVar[str] = ""
    timeout: ClassVar[int] = 60
    labels: ClassVar[tuple[str, ...]] = ()
    catalog_exclude: ClassVar[bool] = False
    # Test-plan test IDs (e.g. "SEC01-01") this validation implements, used to
    # join the code to docs/test-plan.yaml without git/PR archaeology. Declare
    # ``(UNMAPPED,)`` when a check intentionally maps to no plan entry; an empty
    # tuple is treated as "not yet linked" and fails the completeness guardrail.
    test_ids: ClassVar[tuple[str, ...]] = ()

    def __init__(self, runner: Runner | None = None, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.name = self.__class__.__name__
        self.runner = runner or LocalRunner()
        self._passed = False
        self._output = ""
        self._error = ""
        self._results: list[CommandResult] = []
        self._subtests: SubTests | None = None  # Injected by test framework
        self._subtest_results: list[dict[str, Any]] = []  # Track subtest outcomes
        self.log = setup_logger(self.name)

    @abstractmethod
    def run(self) -> None:
        """Implement the validation logic here.

        Use self.run_command() to execute commands.
        Use self.set_passed() or self.set_failed() to set the result.
        """
        pass

    def run_command(self, cmd: str, timeout: int | None = None, display_cmd: str | None = None) -> CommandResult:
        """Run a command using the configured runner.

        Args:
            cmd: The actual command to execute.
            timeout: Command timeout in seconds (defaults to self.timeout).
            display_cmd: Optional human-readable version for logging. Useful when
                        the actual command contains encoded/obfuscated content.
        """
        if timeout is None:
            timeout = self.timeout

        self.log.info(f"Running command: {display_cmd or cmd}")
        result = self.runner.run(cmd, timeout=timeout)
        self._results.append(result)
        self.log.debug(f"Command result: {result}")
        return result

    def set_passed(self, message: str = "") -> None:
        """Mark the validation as passed.

        Call this method from your ``run()`` implementation when the validation
        succeeds.

        Args:
            message: Optional success message describing what was validated.
                This will be included in the test output.

        Example:
            >>> self.set_passed("Found 8 GPUs across 2 nodes")
        """
        self._passed = True
        if message:
            self._output = message

    def set_failed(self, error: str, output: str = "") -> None:
        """Mark the validation as failed.

        Call this method from your ``run()`` implementation when the validation
        fails.

        Args:
            error: Error message describing why the validation failed.
                This should be actionable and help users understand the issue.
            output: Optional additional output (e.g., command output, logs)
                that provides context for debugging.

        Example:
            >>> self.set_failed("Expected 8 GPUs but found 4", output=nvidia_smi_output)
        """
        self._passed = False
        self._error = error
        if output:
            self._output = output

    def _parse_positive_int(self, key: str, *, default: int) -> int | None:
        """Read ``key`` from config and coerce to an integer >= 1.

        Returns the value on success, or ``None`` after calling ``set_failed``
        if the value is missing, not integer-coercible, or less than 1.
        """
        raw = self.config.get(key, default)
        if isinstance(raw, bool):
            self.set_failed(f"`{key}` must be an integer, got bool: {raw!r}")
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            self.set_failed(f"`{key}` must be an integer, got {type(raw).__name__}: {raw!r}")
            return None
        if value < 1:
            self.set_failed(f"{key} must be >= 1 (got {value})")
            return None
        return value

    def report_subtest(
        self,
        name: str,
        passed: bool,
        message: str = "",
        *,
        skipped: bool = False,
        duration: float | None = None,
    ) -> None:
        """Report a subtest result.

        If subtests fixture is available (via _subtests), the subtest will be
        reported to pytest. Otherwise, results are stored for summary.

        Args:
            name: Name of the subtest (e.g., "TestGPUAccess")
            passed: True if subtest passed, False if failed
            message: Optional message describing the result
            skipped: If True, mark as skipped instead of pass/fail
            duration: Optional duration in seconds

        Example:
            >>> self.report_subtest("TestGPUAccess", True, "GPU accessible")
            >>> self.report_subtest("TestNCCL", False, "NCCL test failed: timeout")
            >>> self.report_subtest("TestEFA", True, skipped=True)
        """
        import pytest

        # Normalize: skipped tests are never considered "passed"
        effective_passed = False if skipped else passed

        result = {
            "name": name,
            "passed": effective_passed,
            "skipped": skipped,
            "message": message,
            "duration": duration,
        }
        self._subtest_results.append(result)

        # Report via subtests fixture if available
        if self._subtests is not None:
            # Use skipped=skipped parameter to avoid pytest.skip() adding markers to parent.
            # message= surfaces in the report's <skipped> body when skipped=True;
            # the failure path uses pytest.fail(message) below.
            with self._subtests.test(msg=name, duration=duration, skipped=skipped, message=message or None):
                if not skipped and not passed:
                    pytest.fail(message or f"Subtest {name} failed")
                # If passed or skipped, just exit the context successfully

    def execute(self) -> dict[str, Any]:
        """Execute the validation and return results.

        This method is called by the test framework to run the validation.
        It wraps the ``run()`` method with timing and exception handling.

        Returns:
            Dictionary containing:
                - name: Validation class name
                - passed: True if validation succeeded
                - output: Success message or additional output
                - error: Error message if validation failed
                - duration: Execution time in seconds
                - description: Validation description from class metadata
                - subtests: List of subtest results (if any)
        """
        from isvtest.core.resolution import ErrorReason

        start_time = time.time()
        error_reason: str | None = None
        try:
            self.run()
        except Exception as e:
            self.set_failed(f"Validation raised exception: {e}")
            error_reason = ErrorReason.RUNTIME_EXCEPTION.value
            self.log.exception("Validation execution failed")

        duration = time.time() - start_time

        return {
            "name": self.name,
            "passed": self._passed,
            "output": self._output,
            "error": self._error,
            "duration": duration,
            "description": self.description,
            "subtests": self._subtest_results,
            "error_reason": error_reason,
        }

    @property
    def passed(self) -> bool:
        """Return whether the validation passed."""
        return self._passed

    @property
    def message(self) -> str:
        """Return the result message (output or error)."""
        return self._output if self._passed else self._error


def _discover_validation_classes() -> dict[str, type[BaseValidation]]:
    """Discover all validation classes and cache them.

    Returns:
        Dictionary mapping class names to classes
    """
    global _validation_class_cache
    if _validation_class_cache is not None:
        return _validation_class_cache

    _validation_class_cache = {}

    try:
        from isvtest.core.discovery import discover_all_tests  # lazy: circular with discovery.py

        for cls in discover_all_tests():
            _validation_class_cache[cls.__name__] = cls
            _logger.debug(f"Discovered validation: {cls.__name__}")

    except Exception as e:
        _logger.warning(f"Failed to discover validations: {e}")

    return _validation_class_cache


def get_validation_class(name: str) -> type[BaseValidation] | None:
    """Get a validation class by name.

    Args:
        name: Class name (e.g., "NodeCountCheck")

    Returns:
        Validation class if found, None otherwise
    """
    cache = _discover_validation_classes()
    return cache.get(name)


def _normalize_metadata_values(values: object) -> tuple[str, ...]:
    """Return metadata values as a tuple of strings."""
    if not values:
        return ()
    if isinstance(values, str):
        return (values,)
    try:
        return tuple(str(value) for value in values)
    except TypeError:
        return (str(values),)


def get_validation_labels(cls: type[BaseValidation]) -> tuple[str, ...]:
    """Return public labels for a validation class."""
    return _normalize_metadata_values(getattr(cls, "labels", ()))


def get_validation_test_ids(cls: type[BaseValidation]) -> tuple[str, ...]:
    """Return the test-plan test IDs a validation class declares it implements."""
    return _normalize_metadata_values(getattr(cls, "test_ids", ()))


def register_validation_class(cls: type[BaseValidation]) -> None:
    """Register a validation class for discovery.

    This allows dynamically adding validation classes that can be
    referenced by name in step configurations.

    Args:
        cls: Validation class to register
    """
    cache = _discover_validation_classes()
    cache[cls.__name__] = cls
