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

"""Tests for applying persisted user config during `isvctl test`."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from isvctl.cli.test import app
from isvctl.config.user import get_config_path

runner = CliRunner()


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point user config at a temp dir and clear NICO_API_BASE before/after each test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ISVCTL_CONFIG", raising=False)
    monkeypatch.delenv("ISVCTL_SECRETS", raising=False)
    monkeypatch.delenv("NICO_API_BASE", raising=False)
    yield tmp_path
    # apply_user_env writes os.environ directly; undo any leak for other tests.
    os.environ.pop("NICO_API_BASE", None)


_RUN_CONFIG = """
commands:
  kubernetes:
    phases: [test]
    steps:
      - name: test_step
        command: echo
        args: ['{"success": true}']
        phase: test
tests:
  platform: kubernetes
  validations: {}
"""


def _write_user_config(value: str) -> None:
    """Write a config.yml persisting ``nico.api_base`` to ``value``."""
    config = get_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"nico:\n  api_base: {value}\n")


def _write_run_config(tmp_path: Path) -> Path:
    """Write a minimal valid run config and return its path."""
    cfg = tmp_path / "run.yaml"
    cfg.write_text(_RUN_CONFIG, encoding="utf-8")
    return cfg


def test_run_applies_user_config(isolated_env: Path, tmp_path: Path) -> None:
    """`test run` applies a persisted var that isn't already exported."""
    _write_user_config("https://from-file.example.com")
    cfg = _write_run_config(tmp_path)
    result = runner.invoke(app, ["run", "-f", str(cfg), "--no-upload", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert os.environ.get("NICO_API_BASE") == "https://from-file.example.com"


def test_process_env_wins_over_file(isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported var takes precedence over the persisted file value."""
    monkeypatch.setenv("NICO_API_BASE", "https://from-shell.example.com")
    _write_user_config("https://from-file.example.com")
    cfg = _write_run_config(tmp_path)
    result = runner.invoke(app, ["run", "-f", str(cfg), "--no-upload", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert os.environ["NICO_API_BASE"] == "https://from-shell.example.com"


def test_no_user_config_skips_loading(isolated_env: Path, tmp_path: Path) -> None:
    """`--no-user-config` leaves the persisted file unapplied."""
    _write_user_config("https://from-file.example.com")
    cfg = _write_run_config(tmp_path)
    result = runner.invoke(app, ["run", "-f", str(cfg), "--no-upload", "--dry-run", "--no-user-config"])
    assert result.exit_code == 0, result.output
    assert os.environ.get("NICO_API_BASE") is None


def test_invalid_user_config_exits_before_running(isolated_env: Path, tmp_path: Path) -> None:
    """A malformed user config fails the run with a clean error before work begins."""
    config = get_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("nico:\n  client_secret: leaked\n")  # secret in config.yml
    cfg = _write_run_config(tmp_path)
    result = runner.invoke(app, ["run", "-f", str(cfg), "--no-upload", "--dry-run"])
    assert result.exit_code == 1
    assert "Failed to load user config" in (result.stderr or result.output)


def test_validate_applies_user_config(isolated_env: Path, tmp_path: Path) -> None:
    """`test validate` also applies persisted user config."""
    _write_user_config("https://from-file.example.com")
    cfg = _write_run_config(tmp_path)
    result = runner.invoke(app, ["validate", "-f", str(cfg)])
    assert result.exit_code == 0, result.output
    assert os.environ.get("NICO_API_BASE") == "https://from-file.example.com"
