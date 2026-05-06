# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for yaml2json.py script."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "yaml2json.py"


def run_yaml2json(yaml_file: str) -> tuple[int, str, str]:
    """Run yaml2json.py and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), yaml_file],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _write_yaml(tmp_path: Path, content: str, name: str = "input.yaml") -> str:
    """Write YAML content to a temporary file and return its path.

    Args:
        tmp_path: Temporary directory where the YAML file is written.
        content: YAML content to write.
        name: YAML filename to create.

    Returns:
        String path to the written YAML file.
    """
    path = tmp_path / name
    path.write_text(content)
    return str(path)


class TestYaml2Json:
    """Tests for yaml2json script."""

    def test_valid_yaml(self, tmp_path: Path) -> None:
        """Test converting valid YAML to JSON."""
        yaml_file = _write_yaml(tmp_path, "key: value\nnumber: 42\n")

        exit_code, stdout, stderr = run_yaml2json(yaml_file)
        assert exit_code == 0
        assert stderr == ""
        data = json.loads(stdout)
        assert data == {"key": "value", "number": 42}

    def test_empty_yaml(self, tmp_path: Path) -> None:
        """Test handling empty YAML file."""
        yaml_file = _write_yaml(tmp_path, "")

        exit_code, stdout, stderr = run_yaml2json(yaml_file)
        assert exit_code == 0
        assert stderr == ""
        data = json.loads(stdout)
        assert data == {}

    def test_yaml_with_datetime(self, tmp_path: Path) -> None:
        """Test handling unquoted YAML timestamp (datetime type)."""
        # Unquoted dates in YAML are parsed as datetime objects
        yaml_file = _write_yaml(tmp_path, "created: 2025-01-14\nupdated: 2025-01-14T10:30:00\n")

        exit_code, stdout, stderr = run_yaml2json(yaml_file)
        assert exit_code == 0
        assert stderr == ""
        data = json.loads(stdout)
        # datetime objects should be serialized as strings
        assert "created" in data
        assert "updated" in data
        assert "2025-01-14" in data["created"]

    def test_file_not_found(self) -> None:
        """Test handling non-existent file."""
        exit_code, _stdout, stderr = run_yaml2json("/nonexistent/file.yaml")
        assert exit_code == 1
        assert "File not found" in stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="Root can read any file regardless of permissions")
    def test_permission_error(self, tmp_path: Path) -> None:
        """Test handling file permission errors."""
        yaml_file = _write_yaml(tmp_path, "key: value\n")
        os.chmod(yaml_file, 0)

        exit_code, _stdout, stderr = run_yaml2json(yaml_file)
        assert exit_code == 1
        assert "Cannot read file" in stderr or "Permission denied" in stderr

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        """Test handling invalid YAML syntax."""
        yaml_file = _write_yaml(tmp_path, "key: [unclosed bracket\n")

        exit_code, _stdout, stderr = run_yaml2json(yaml_file)
        assert exit_code == 1
        assert "Invalid YAML" in stderr

    def test_is_a_directory(self, tmp_path: Path) -> None:
        """Test handling when path is a directory."""
        exit_code, _stdout, stderr = run_yaml2json(str(tmp_path))
        assert exit_code == 1
        assert "Cannot read file" in stderr or "Is a directory" in stderr

    def test_no_arguments(self) -> None:
        """Test handling missing arguments."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "Usage:" in result.stderr
