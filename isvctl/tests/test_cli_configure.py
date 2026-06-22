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

"""Tests for the interactive `isvctl configure` command."""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from isvctl.cli.config import app
from isvctl.config.env_catalog import vars_for_provider
from isvctl.config.user import file_mode, get_config_path, get_secrets_path

runner = CliRunner()

# Enough blank answers to walk every interactive prompt (one per persistable
# var). Derived from the catalog so it can't fall behind as vars are added.
_PERSISTABLE_PROMPTS = sum(1 for var in vars_for_provider(None) if var.persistable)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ISVCTL_CONFIG", raising=False)
    monkeypatch.delenv("ISVCTL_SECRETS", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# path / show
# ---------------------------------------------------------------------------


def test_path_prints_both_files(isolated_env: Path) -> None:
    result = runner.invoke(app, ["path"])
    assert result.exit_code == 0
    assert str(get_config_path()) in result.stdout
    assert str(get_secrets_path()) in result.stdout


def test_show_with_no_files(isolated_env: Path) -> None:
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "No configuration found" in result.stdout


def test_malformed_file_errors_cleanly_not_traceback(isolated_env: Path) -> None:
    # A bad persisted file must surface a clean error (exit 1), not a traceback —
    # `configure`/`show` are how users fix a broken file.
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  client_secret: leaked\n")  # secret in config.yml

    for argv in (["show"], []):
        result = runner.invoke(app, argv, input="\n" * _PERSISTABLE_PROMPTS)
        assert result.exit_code == 1, result.output
        assert "Failed to read user config" in (result.stderr or result.output)
        assert "Traceback" not in result.output


def test_show_prints_nonsecret_and_masks_secret(isolated_env: Path) -> None:
    config = get_config_path()
    secrets = get_secrets_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  api_base: https://nico.example.com\n")
    secrets.write_text("nico:\n  client_secret: super-secret-value\n")

    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "https://nico.example.com" in result.stdout
    assert "super-secret-value" not in result.stdout
    assert "(set)" in result.stdout


# ---------------------------------------------------------------------------
# wizard
# ---------------------------------------------------------------------------


def test_wizard_writes_both_files(isolated_env: Path) -> None:
    # Answer only the two NICo vars we care about; Enter (blank) skips the rest.
    nico_vars = vars_for_provider("nico")
    answers = []
    for var in nico_vars:
        if var.name == "NICO_API_BASE":
            answers.append("https://nico.example.com")
        elif var.name == "NICO_CLIENT_SECRET":
            answers.append("shhh")
        else:
            answers.append("")
    result = runner.invoke(app, ["--provider", "nico"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    secrets_data = yaml.safe_load(get_secrets_path().read_text())
    assert config_data["nico"]["api_base"] == "https://nico.example.com"
    assert secrets_data["nico"]["client_secret"] == "shhh"


def test_wizard_secrets_file_is_0600(isolated_env: Path) -> None:
    nico_vars = vars_for_provider("nico")
    answers = ["shhh" if var.name == "NICO_CLIENT_SECRET" else "" for var in nico_vars]
    result = runner.invoke(app, ["--provider", "nico"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, result.stdout
    assert file_mode(get_secrets_path()) == 0o600


def test_wizard_blank_keeps_existing(isolated_env: Path) -> None:
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  api_base: https://keep.example.com\n")

    nico_vars = vars_for_provider("nico")
    # All blank → keep everything as-is.
    result = runner.invoke(app, ["--provider", "nico"], input="\n" * len(nico_vars))
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["api_base"] == "https://keep.example.com"


def test_wizard_only_prompts_provider_group(isolated_env: Path) -> None:
    nico_vars = vars_for_provider("nico")
    result = runner.invoke(app, ["--provider", "nico"], input="\n" * len(nico_vars))
    assert result.exit_code == 0, result.stdout
    assert "NICO_API_BASE" in result.stdout
    assert "AWS_REGION" not in result.stdout


def test_unknown_provider_errors(isolated_env: Path) -> None:
    result = runner.invoke(app, ["--provider", "gcp"], input="\n")
    assert result.exit_code == 2
    assert "unknown provider" in (result.stderr or result.output)


def test_wizard_never_prompts_flags(isolated_env: Path) -> None:
    # Bare wizard walks every persistable var; flags must not appear.
    result = runner.invoke(app, [], input="\n" * _PERSISTABLE_PROMPTS)
    assert result.exit_code == 0, result.stdout
    for flag in ("KUBECTL", "ISVCTL_DEMO_MODE", "ISVTEST_INCLUDE_UNRELEASED", "AWS_SKIP_TEARDOWN", "Flags"):
        assert flag not in result.stdout


def test_show_never_lists_flags(isolated_env: Path) -> None:
    # Even if a flag somehow lands in a file, show resolves only persistable vars.
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  api_base: https://x\n")
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "KUBECTL" not in result.stdout
    assert "api_base" in result.stdout
