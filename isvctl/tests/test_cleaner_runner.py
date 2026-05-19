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

"""Unit tests for operation runner."""

from typing import Any
from unittest.mock import MagicMock, patch

from isvctl.cleaner.runner import OperationRunner


def mock_successful_operation() -> dict[str, Any]:
    """Mock operation that succeeds."""
    return {"success": True, "message": "Mock operation succeeded"}


def mock_failing_operation() -> dict[str, Any]:
    """Mock operation that fails."""
    return {"success": False, "message": "Mock operation failed"}


def mock_exception_operation() -> dict[str, Any]:
    """Mock operation that raises an exception."""
    raise RuntimeError("Mock exception")


def test_runner_initialization() -> None:
    """Test OperationRunner initialization."""
    runner = OperationRunner()
    assert runner.verbose is False
    assert runner.dry_run is False
    assert runner.continue_on_error is False

    runner_verbose = OperationRunner(verbose=True, dry_run=True, continue_on_error=True)
    assert runner_verbose.verbose is True
    assert runner_verbose.dry_run is True
    assert runner_verbose.continue_on_error is True


def test_runner_successful_operations() -> None:
    """Test runner with successful operations."""
    runner = OperationRunner()

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_successful_operation, "First mock operation"),
            "mock-op-2": (mock_successful_operation, "Second mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "mock-op-2"])

    assert len(results) == 2
    assert all(r["success"] for r in results)
    assert results[0]["operation"] == "mock-op-1"
    assert results[1]["operation"] == "mock-op-2"


def test_runner_failing_operation_stops_execution() -> None:
    """Test that runner stops on first failure by default."""
    runner = OperationRunner()

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_successful_operation, "First mock operation"),
            "mock-op-2": (mock_failing_operation, "Failing operation"),
            "mock-op-3": (mock_successful_operation, "Third mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "mock-op-2", "mock-op-3"])

    # Should stop after the failing operation
    assert len(results) == 2
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    # Third operation should not have run


def test_runner_continue_on_error() -> None:
    """Test that runner continues on error when flag is set."""
    runner = OperationRunner(continue_on_error=True)

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_successful_operation, "First mock operation"),
            "mock-op-2": (mock_failing_operation, "Failing operation"),
            "mock-op-3": (mock_successful_operation, "Third mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "mock-op-2", "mock-op-3"])

    # Should run all three operations
    assert len(results) == 3
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert results[2]["success"] is True


def test_runner_exception_handling() -> None:
    """Test runner handles exceptions properly."""
    runner = OperationRunner()

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_successful_operation, "First mock operation"),
            "mock-op-exception": (mock_exception_operation, "Exception operation"),
            "mock-op-3": (mock_successful_operation, "Third mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "mock-op-exception", "mock-op-3"])

    # Should stop after the exception
    assert len(results) == 2
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert "error" in results[1]
    assert "Mock exception" in results[1]["error"]


def test_runner_exception_with_continue_on_error() -> None:
    """Test runner continues after exception when flag is set."""
    runner = OperationRunner(continue_on_error=True)

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_successful_operation, "First mock operation"),
            "mock-op-exception": (mock_exception_operation, "Exception operation"),
            "mock-op-3": (mock_successful_operation, "Third mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "mock-op-exception", "mock-op-3"])

    # Should run all three operations
    assert len(results) == 3
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert results[2]["success"] is True


def test_runner_dry_run() -> None:
    """Test runner in dry-run mode."""
    runner = OperationRunner(dry_run=True)

    # Use a mock that would fail if actually called
    mock_op = MagicMock(side_effect=RuntimeError("Should not be called in dry run"))

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_op, "Mock operation"),
            "mock-op-2": (mock_op, "Another mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "mock-op-2"])

    # Operations should not have been called
    mock_op.assert_not_called()

    # But results should show success
    assert len(results) == 2
    assert all(r["success"] for r in results)
    assert all("Dry run" in r.get("message", "") for r in results)


def test_runner_unknown_operation() -> None:
    """Test runner with unknown operation name."""
    runner = OperationRunner()

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_successful_operation, "Mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "unknown-operation"])

    # Should run first operation and fail on unknown
    assert len(results) == 2
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert "Unknown operation" in results[1]["error"]


def test_runner_unknown_operation_with_continue() -> None:
    """Test runner continues after unknown operation when flag is set."""
    runner = OperationRunner(continue_on_error=True)

    with patch(
        "isvctl.cleaner.runner.OPERATIONS",
        {
            "mock-op-1": (mock_successful_operation, "First mock operation"),
            "mock-op-3": (mock_successful_operation, "Third mock operation"),
        },
    ):
        results = runner.run_operations(["mock-op-1", "unknown-operation", "mock-op-3"])

    # Should run all three (with middle one failing)
    assert len(results) == 3
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert results[2]["success"] is True


def test_runner_verbose_mode() -> None:
    """Test runner in verbose mode logs additional information."""
    runner = OperationRunner(verbose=True)

    with (
        patch(
            "isvctl.cleaner.runner.OPERATIONS",
            {
                "mock-op-1": (mock_successful_operation, "Mock operation"),
            },
        ),
        patch("isvctl.cleaner.runner.logger") as mock_logger,
    ):
        results = runner.run_operations(["mock-op-1"])

    assert len(results) == 1
    assert results[0]["success"] is True

    # Check that verbose logging occurred
    assert mock_logger.info.call_count >= 3  # At least operation name, description, and result
