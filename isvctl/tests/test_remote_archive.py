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

"""Tests for the archive module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from isvctl.remote.archive import ArchiveError, TarArchive


class TestTarArchive:
    """Tests for TarArchive class."""

    def test_init_default(self) -> None:
        """Test default initialization."""
        archive = TarArchive()
        assert archive.working_dir == Path.cwd()

    def test_init_with_working_dir(self, tmp_path: Path) -> None:
        """Test initialization with custom working directory."""
        archive = TarArchive(working_dir=tmp_path)
        assert archive.working_dir == tmp_path

    def test_default_excludes(self) -> None:
        """Test default exclude patterns."""
        from isvctl.remote.archive import DEFAULT_EXCLUDES

        expected = [
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "*.pyc",
            ".git",
            ".terraform",
            "*.tfstate",
            "*.tfstate.backup",
        ]
        assert DEFAULT_EXCLUDES == expected

    @patch("subprocess.run")
    def test_create_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test successful archive creation."""
        # Create test directories and files
        test_dir = tmp_path / "project"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        output = tmp_path / "archive.tar.gz"
        # Simulate tar creating the file
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Create the output file to simulate successful tar
        output.write_bytes(b"fake tar content")

        archive = TarArchive(working_dir=tmp_path)
        result = archive.create(output=output, paths=["project"])

        assert result == output
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_create_with_excludes(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test archive creation with custom excludes."""
        test_dir = tmp_path / "project"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        output = tmp_path / "archive.tar.gz"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output.write_bytes(b"fake tar content")

        archive = TarArchive(working_dir=tmp_path)
        archive.create(output=output, paths=["project"], excludes=[".git", "*.log"])

        # Check that tar was called with exclude flags
        call_args = mock_run.call_args[0][0]
        assert "--exclude" in call_args
        assert ".git" in call_args
        assert "*.log" in call_args

    @patch("subprocess.run")
    def test_create_no_compression(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test archive creation without compression."""
        test_dir = tmp_path / "project"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        output = tmp_path / "archive.tar"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output.write_bytes(b"fake tar content")

        archive = TarArchive(working_dir=tmp_path)
        archive.create(output=output, paths=["project"], compress=False)

        call_args = mock_run.call_args[0][0]
        assert "-cf" in call_args
        assert "-czf" not in " ".join(call_args)

    def test_create_path_not_found(self, tmp_path: Path) -> None:
        """Test archive creation with nonexistent path."""
        archive = TarArchive(working_dir=tmp_path)

        with pytest.raises(ArchiveError) as exc_info:
            archive.create(
                output=tmp_path / "archive.tar.gz",
                paths=["nonexistent"],
            )

        assert "not found" in str(exc_info.value).lower()

    @patch("subprocess.run")
    def test_create_tar_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test handling tar command failure."""
        test_dir = tmp_path / "project"
        test_dir.mkdir()

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="tar: Error creating archive",
        )

        archive = TarArchive(working_dir=tmp_path)

        with pytest.raises(ArchiveError) as exc_info:
            archive.create(output=tmp_path / "archive.tar.gz", paths=["project"])

        assert "failed" in str(exc_info.value).lower()

    @patch("subprocess.run")
    def test_create_tar_not_found(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test handling when tar command is not found."""
        test_dir = tmp_path / "project"
        test_dir.mkdir()

        mock_run.side_effect = FileNotFoundError("tar not found")

        archive = TarArchive(working_dir=tmp_path)

        with pytest.raises(ArchiveError) as exc_info:
            archive.create(output=tmp_path / "archive.tar.gz", paths=["project"])

        assert "not found" in str(exc_info.value).lower()

    def test_format_size_bytes(self) -> None:
        """Test size formatting for bytes."""
        archive = TarArchive()
        assert archive._format_size(500) == "500.0B"

    def test_format_size_kb(self) -> None:
        """Test size formatting for kilobytes."""
        archive = TarArchive()
        assert archive._format_size(2048) == "2.0KB"

    def test_format_size_mb(self) -> None:
        """Test size formatting for megabytes."""
        archive = TarArchive()
        assert archive._format_size(5 * 1024 * 1024) == "5.0MB"

    def test_format_size_gb(self) -> None:
        """Test size formatting for gigabytes."""
        archive = TarArchive()
        assert archive._format_size(2 * 1024 * 1024 * 1024) == "2.0GB"

    @patch("subprocess.run")
    def test_create_sets_copyfile_disable(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test that COPYFILE_DISABLE is set for macOS compatibility."""
        test_dir = tmp_path / "project"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        output = tmp_path / "archive.tar.gz"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output.write_bytes(b"fake tar content")

        archive = TarArchive(working_dir=tmp_path)
        archive.create(output=output, paths=["project"])

        # Check that COPYFILE_DISABLE is in the environment
        call_kwargs = mock_run.call_args[1]
        assert "env" in call_kwargs
        assert call_kwargs["env"].get("COPYFILE_DISABLE") == "1"
