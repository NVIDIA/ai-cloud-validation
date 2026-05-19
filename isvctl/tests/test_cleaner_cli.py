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

"""Unit tests for clean CLI subcommand."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from isvctl.cli.clean import app

runner = CliRunner()


def test_clean_help() -> None:
    """Test clean subcommand help."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ISV Lab clean-up operations" in result.output


def test_clean_run_help() -> None:
    """Test clean run command help."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "Run ISV Lab clean-up operations" in result.output


def test_clean_list() -> None:
    """Test clean list command."""
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "Available clean-up operations" in result.output
    assert "firmware-validation" in result.output
    assert "firmware-flashing" in result.output
    assert "network-reset" in result.output
    assert "bcm-validation" in result.output
    assert "os-reimage" in result.output
    assert "os-config" in result.output


def test_clean_run_single_operation() -> None:
    """Test running a single clean operation."""
    mock_results = [{"operation": "firmware-validation", "success": True}]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(app, ["run", "firmware-validation"])

        mock_runner_class.assert_called_once_with(
            verbose=False,
            dry_run=False,
            continue_on_error=False,
        )
        mock_runner.run_operations.assert_called_once_with(["firmware-validation"])

    assert result.exit_code == 0
    assert "All operations completed successfully" in result.output


def test_clean_run_multiple_operations() -> None:
    """Test running multiple clean operations."""
    mock_results = [
        {"operation": "firmware-validation", "success": True},
        {"operation": "network-reset", "success": True},
    ]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(app, ["run", "firmware-validation", "network-reset"])

        mock_runner.run_operations.assert_called_once_with(["firmware-validation", "network-reset"])

    assert result.exit_code == 0


def test_clean_run_all_operations() -> None:
    """Test running all clean operations."""
    mock_results = [
        {"operation": "firmware-validation", "success": True},
        {"operation": "firmware-flashing", "success": True},
        {"operation": "network-reset", "success": True},
        {"operation": "bcm-validation", "success": True},
        {"operation": "os-reimage", "success": True},
        {"operation": "os-config", "success": True},
    ]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(app, ["run", "all"])

        # Should expand 'all' to list of all operations
        called_operations = mock_runner.run_operations.call_args[0][0]
        assert len(called_operations) == 6

    assert result.exit_code == 0


def test_clean_run_with_failure() -> None:
    """Test running operations with failure."""
    mock_results = [
        {"operation": "firmware-validation", "success": True},
        {"operation": "network-reset", "success": False, "error": "Network error"},
    ]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(app, ["run", "firmware-validation", "network-reset"])

    assert result.exit_code == 1
    assert "1 operation(s) failed" in result.output


def test_clean_run_verbose() -> None:
    """Test running with verbose flag."""
    mock_results = [{"operation": "firmware-validation", "success": True}]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(app, ["run", "firmware-validation", "--verbose"])

        mock_runner_class.assert_called_once_with(
            verbose=True,
            dry_run=False,
            continue_on_error=False,
        )

    assert result.exit_code == 0


def test_clean_run_dry_run() -> None:
    """Test running with dry-run flag."""
    mock_results = [{"operation": "firmware-validation", "success": True}]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(app, ["run", "firmware-validation", "--dry-run"])

        mock_runner_class.assert_called_once_with(
            verbose=False,
            dry_run=True,
            continue_on_error=False,
        )

    assert result.exit_code == 0


def test_clean_run_continue_on_error() -> None:
    """Test running with continue-on-error flag."""
    mock_results = [
        {"operation": "firmware-validation", "success": True},
        {"operation": "network-reset", "success": False},
        {"operation": "os-config", "success": True},
    ]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(app, ["run", "all", "--continue-on-error"])

        mock_runner_class.assert_called_once_with(
            verbose=False,
            dry_run=False,
            continue_on_error=True,
        )

    # Should still exit with failure since one operation failed
    assert result.exit_code == 1


def test_clean_run_combined_flags() -> None:
    """Test running with multiple flags combined."""
    mock_results = [{"operation": "firmware-validation", "success": True}]

    with patch("isvctl.cli.clean.OperationRunner") as mock_runner_class:
        mock_runner = MagicMock()
        mock_runner.run_operations.return_value = mock_results
        mock_runner_class.return_value = mock_runner

        result = runner.invoke(
            app,
            ["run", "firmware-validation", "-v", "--dry-run", "--continue-on-error"],
        )

        mock_runner_class.assert_called_once_with(
            verbose=True,
            dry_run=True,
            continue_on_error=True,
        )

    assert result.exit_code == 0


def test_clean_run_unknown_operation() -> None:
    """Test running with unknown operation name."""
    result = runner.invoke(app, ["run", "unknown-operation"])

    assert result.exit_code == 1
    assert "Unknown operation" in result.output
