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

"""Tests for the ISV Lab Service API client."""

from pathlib import Path
from unittest.mock import patch

from isvreporter.client import calculate_duration, load_test_run_id


class TestCalculateDuration:
    """Tests for calculate_duration function."""

    def test_calculate_duration(self) -> None:
        """Test duration calculation from ISO 8601 timestamp."""
        # This is a basic smoke test - more comprehensive tests would use mocking
        # for the actual API calls
        start_time = "2024-01-01T12:00:00Z"
        duration = calculate_duration(start_time)

        # Duration should be positive (we're calculating from past to now)
        assert duration > 0
        assert isinstance(duration, int)

    def test_calculate_duration_with_timezone(self) -> None:
        """Test duration calculation with explicit timezone."""
        start_time = "2024-01-01T12:00:00+00:00"
        duration = calculate_duration(start_time)

        assert duration > 0
        assert isinstance(duration, int)


class TestLoadTestRunId:
    """Tests for load_test_run_id function."""

    def test_load_existing_test_run_id(self, tmp_path: Path) -> None:
        """Test loading test run ID from existing file."""
        # Create test file
        output_dir = tmp_path / "_output"
        output_dir.mkdir()
        test_run_file = output_dir / "testrun_id.txt"
        test_run_file.write_text("test-run-12345")

        with patch("isvreporter.client.TEST_RUN_ID_FILE", test_run_file):
            result = load_test_run_id()
            assert result == "test-run-12345"

    def test_load_test_run_id_strips_whitespace(self, tmp_path: Path) -> None:
        """Test that whitespace is stripped from test run ID."""
        output_dir = tmp_path / "_output"
        output_dir.mkdir()
        test_run_file = output_dir / "testrun_id.txt"
        test_run_file.write_text("  test-run-67890  \n")

        with patch("isvreporter.client.TEST_RUN_ID_FILE", test_run_file):
            result = load_test_run_id()
            assert result == "test-run-67890"

    def test_load_test_run_id_file_not_found(self, tmp_path: Path) -> None:
        """Test that None is returned when file doesn't exist."""
        nonexistent_file = tmp_path / "_output" / "testrun_id.txt"

        with patch("isvreporter.client.TEST_RUN_ID_FILE", nonexistent_file):
            result = load_test_run_id()
            assert result is None

    def test_load_empty_test_run_id(self, tmp_path: Path) -> None:
        """Test loading empty test run ID file."""
        output_dir = tmp_path / "_output"
        output_dir.mkdir()
        test_run_file = output_dir / "testrun_id.txt"
        test_run_file.write_text("")

        with patch("isvreporter.client.TEST_RUN_ID_FILE", test_run_file):
            result = load_test_run_id()
            assert result == ""
