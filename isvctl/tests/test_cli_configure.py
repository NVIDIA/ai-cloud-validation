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
    """Point configure commands at an isolated user config directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ISVCTL_CONFIG", raising=False)
    monkeypatch.delenv("ISVCTL_SECRETS", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# path / show
# ---------------------------------------------------------------------------


def test_path_prints_both_files(isolated_env: Path) -> None:
    """The path command prints both persisted config file locations."""
    result = runner.invoke(app, ["path"])
    assert result.exit_code == 0
    assert str(get_config_path()) in result.stdout
    assert str(get_secrets_path()) in result.stdout


def test_show_with_no_files(isolated_env: Path) -> None:
    """The show command reports when no persisted config exists."""
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "No configuration found" in result.stdout


def test_malformed_file_errors_cleanly_not_traceback(isolated_env: Path) -> None:
    """Malformed persisted config fails cleanly without a traceback."""
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
    """The show command prints non-secrets and masks secrets."""
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
    """The wizard writes non-secret and secret answers to separate files."""
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
    """The wizard writes secrets.yml with owner-only permissions."""
    nico_vars = vars_for_provider("nico")
    answers = ["shhh" if var.name == "NICO_CLIENT_SECRET" else "" for var in nico_vars]
    result = runner.invoke(app, ["--provider", "nico"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, result.stdout
    assert file_mode(get_secrets_path()) == 0o600


def test_wizard_blank_keeps_existing(isolated_env: Path) -> None:
    """Blank wizard answers preserve existing saved values."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  api_base: https://keep.example.com\n")

    nico_vars = vars_for_provider("nico")
    # All blank → keep everything as-is.
    result = runner.invoke(app, ["--provider", "nico"], input="\n" * len(nico_vars))
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["api_base"] == "https://keep.example.com"


def test_wizard_only_prompts_provider_group(isolated_env: Path) -> None:
    """A provider-scoped wizard prompts only that provider group."""
    nico_vars = vars_for_provider("nico")
    result = runner.invoke(app, ["--provider", "nico"], input="\n" * len(nico_vars))
    assert result.exit_code == 0, result.stdout
    assert "NICO_API_BASE" in result.stdout
    assert "AWS_REGION" not in result.stdout


def test_unknown_provider_errors(isolated_env: Path) -> None:
    """An unknown provider name is rejected before prompting."""
    result = runner.invoke(app, ["--provider", "gcp"], input="\n")
    assert result.exit_code == 2
    assert "unknown provider" in (result.stderr or result.output)


def test_wizard_never_prompts_flags(isolated_env: Path) -> None:
    """The bare wizard never prompts for non-persistable flags."""
    # Bare wizard walks every persistable var; flags must not appear.
    result = runner.invoke(app, [], input="\n" * _PERSISTABLE_PROMPTS)
    assert result.exit_code == 0, result.stdout
    for flag in ("KUBECTL", "ISVCTL_DEMO_MODE", "ISVTEST_INCLUDE_UNRELEASED", "AWS_SKIP_TEARDOWN", "Flags"):
        assert flag not in result.stdout


def test_show_never_lists_flags(isolated_env: Path) -> None:
    """The show command never lists non-persistable flags."""
    # Even if a flag somehow lands in a file, show resolves only persistable vars.
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  api_base: https://x\n")
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "KUBECTL" not in result.stdout
    assert "api_base" in result.stdout


# ---------------------------------------------------------------------------
# set / unset
# ---------------------------------------------------------------------------


def test_set_writes_nonsecret_from_env_name(isolated_env: Path) -> None:
    """The set command accepts an env var name for non-secret values."""
    result = runner.invoke(app, ["set", "NICO_API_BASE", "https://nico.example.com"])
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["api_base"] == "https://nico.example.com"
    assert not get_secrets_path().exists()


def test_set_accepts_section_key_alias(isolated_env: Path) -> None:
    """The set command accepts section.key aliases."""
    result = runner.invoke(app, ["set", "nico.api_base", "https://nico.example.com"])
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["api_base"] == "https://nico.example.com"


def test_set_accepts_section_key_assignment(isolated_env: Path) -> None:
    """The set command accepts section.key=value assignments."""
    result = runner.invoke(app, ["set", "nico.api_base=https://nico.example.com"])
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["api_base"] == "https://nico.example.com"


def test_set_accepts_env_name_assignment(isolated_env: Path) -> None:
    """The set command accepts ENV_NAME=value assignments."""
    result = runner.invoke(app, ["set", "NICO_API_BASE=https://nico.example.com"])
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["api_base"] == "https://nico.example.com"


def test_set_accepts_multiple_assignments(isolated_env: Path) -> None:
    """The set command accepts multiple key=value assignments."""
    result = runner.invoke(app, ["set", "nico.organization=ncx", "nico.oidc_scope=example"])
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["organization"] == "ncx"
    assert config_data["nico"]["oidc_scope"] == "example"


def test_set_split_form_accepts_value_containing_equals(isolated_env: Path) -> None:
    """The split set form preserves equals signs inside the value."""
    result = runner.invoke(app, ["set", "nico.oidc_scope", "audience=example"])
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"]["oidc_scope"] == "audience=example"


def test_set_rejects_assignment_with_extra_value(isolated_env: Path) -> None:
    """The set command rejects key=value combined with a separate value."""
    result = runner.invoke(app, ["set", "nico.organization=ncx", "ignored"])
    assert result.exit_code == 2
    assert "cannot combine key=value with a separate value" in (result.stderr or result.output)


def test_set_rejects_multiple_split_pairs(isolated_env: Path) -> None:
    """The set command rejects multiple split key/value pairs."""
    result = runner.invoke(app, ["set", "nico.organization", "ncx", "nico.oidc_scope", "example"])
    assert result.exit_code == 2
    assert "multiple values must use key=value" in (result.stderr or result.output)


def test_set_prompts_for_secret_without_echoing_value(isolated_env: Path) -> None:
    """The set command prompts for secret values without echoing them."""
    result = runner.invoke(app, ["set", "NICO_CLIENT_SECRET"], input="super-secret-value\n")
    assert result.exit_code == 0, result.stdout

    secrets_data = yaml.safe_load(get_secrets_path().read_text())
    assert secrets_data["nico"]["client_secret"] == "super-secret-value"
    assert "super-secret-value" not in result.output


def test_set_rejects_unknown_key(isolated_env: Path) -> None:
    """The set command rejects unknown section.key names."""
    result = runner.invoke(app, ["set", "nico.not_real", "value"])
    assert result.exit_code == 2
    assert "unknown config key" in (result.stderr or result.output)


def test_set_rejects_per_run_flag_before_loading_config(isolated_env: Path) -> None:
    """The set command rejects flags before reading existing config."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  client_secret: leaked\n")

    result = runner.invoke(app, ["set", "KUBECTL", "kubectl"])
    assert result.exit_code == 2
    assert "per-run flag" in (result.stderr or result.output)
    assert "Failed to read user config" not in result.output


def test_unset_removes_one_saved_key(isolated_env: Path) -> None:
    """The unset command removes one saved env var value."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  api_base: https://nico.example.com\n  organization: example-org\n")

    result = runner.invoke(app, ["unset", "NICO_API_BASE"])
    assert result.exit_code == 0, result.stdout

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["nico"] == {"organization": "example-org"}


def test_unset_accepts_section_key_alias(isolated_env: Path) -> None:
    """The unset command accepts section.key aliases."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  api_base: https://nico.example.com\n")

    result = runner.invoke(app, ["unset", "nico.api_base"])
    assert result.exit_code == 0, result.stdout

    assert not get_config_path().exists()


def test_unset_section_removes_saved_values_after_confirmation(isolated_env: Path) -> None:
    """The unset command removes a whole section after confirmation."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text(
        "aws:\n"
        "  region: us-west-2\n"
        "isv_lab_service:\n"
        "  service_endpoint: https://service.example.com\n"
        "nico:\n"
        "  api_base: https://nico.example.com\n"
        "  organization: example-org\n"
    )
    get_secrets_path().write_text(
        "isv_lab_service:\n  client_secret: service-secret-value\nnico:\n  client_secret: super-secret-value\n"
    )

    result = runner.invoke(app, ["unset", "nico"], input="y\n")
    assert result.exit_code == 0, result.stdout
    assert "api_base" in result.output
    assert "client_secret" in result.output
    assert "super-secret-value" not in result.output
    assert "service-secret-value" not in result.output

    config_data = yaml.safe_load(get_config_path().read_text())
    secrets_data = yaml.safe_load(get_secrets_path().read_text())
    assert "nico" not in config_data
    assert "nico" not in secrets_data
    assert config_data["aws"]["region"] == "us-west-2"
    assert config_data["isv_lab_service"]["service_endpoint"] == "https://service.example.com"
    assert secrets_data["isv_lab_service"]["client_secret"] == "service-secret-value"


def test_unset_section_requires_confirmation(isolated_env: Path) -> None:
    """The unset command preserves a section when confirmation is denied."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  api_base: https://nico.example.com\n")
    get_secrets_path().write_text("nico:\n  client_secret: super-secret-value\n")

    result = runner.invoke(app, ["unset", "nico"], input="n\n")
    assert result.exit_code == 1
    assert "Aborted." in result.output
    assert "super-secret-value" not in result.output

    config_data = yaml.safe_load(get_config_path().read_text())
    secrets_data = yaml.safe_load(get_secrets_path().read_text())
    assert config_data["nico"]["api_base"] == "https://nico.example.com"
    assert secrets_data["nico"]["client_secret"] == "super-secret-value"


def test_unset_section_with_no_saved_values(isolated_env: Path) -> None:
    """The unset command is a no-op for a section with no saved values."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("aws:\n  region: us-west-2\n")

    result = runner.invoke(app, ["unset", "nico"])
    assert result.exit_code == 0, result.stdout
    assert "No saved values for nico." in result.output

    config_data = yaml.safe_load(get_config_path().read_text())
    assert config_data["aws"]["region"] == "us-west-2"


def test_unset_section_rejects_malformed_config_without_deleting(isolated_env: Path) -> None:
    """The unset command leaves files intact when section removal cannot load config."""
    get_config_path().parent.mkdir(parents=True)
    config_body = "nico:\n  client_secret: leaked\n"
    secrets_body = "nico:\n  client_secret: super-secret-value\n"
    get_config_path().write_text(config_body)
    get_secrets_path().write_text(secrets_body)

    result = runner.invoke(app, ["unset", "nico"], input="y\n")
    assert result.exit_code == 1
    assert "Failed to read user config" in (result.stderr or result.output)
    assert "leaked" not in result.output
    assert "super-secret-value" not in result.output
    assert get_config_path().read_text() == config_body
    assert get_secrets_path().read_text() == secrets_body


def test_unset_without_key_requires_all_flag(isolated_env: Path) -> None:
    """The unset command requires a key unless --all is passed."""
    result = runner.invoke(app, ["unset"])
    assert result.exit_code == 2
    assert "provide a key or pass --all" in (result.stderr or result.output)


def test_unset_rejects_per_run_flag_before_loading_config(isolated_env: Path) -> None:
    """The unset command rejects flags before reading existing config."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  client_secret: leaked\n")

    result = runner.invoke(app, ["unset", "KUBECTL"])
    assert result.exit_code == 2
    assert "per-run flag" in (result.stderr or result.output)
    assert "Failed to read user config" not in result.output


def test_unset_all_requires_confirmation(isolated_env: Path) -> None:
    """The unset --all command preserves files when confirmation is denied."""
    write_values = ["set", "NICO_API_BASE", "https://nico.example.com"]
    assert runner.invoke(app, write_values).exit_code == 0

    result = runner.invoke(app, ["unset", "--all"], input="n\n")
    assert result.exit_code == 1
    assert get_config_path().exists()


def test_unset_all_removes_config_after_confirmation(isolated_env: Path) -> None:
    """The unset --all command removes all persisted files after confirmation."""
    assert runner.invoke(app, ["set", "NICO_API_BASE", "https://nico.example.com"]).exit_code == 0
    assert runner.invoke(app, ["set", "NICO_CLIENT_SECRET"], input="super-secret-value\n").exit_code == 0

    result = runner.invoke(app, ["unset", "--all"], input="y\n")
    assert result.exit_code == 0, result.stdout

    assert not get_config_path().exists()
    assert not get_secrets_path().exists()


def test_unset_all_can_clear_malformed_config(isolated_env: Path) -> None:
    """The unset --all command can remove malformed persisted files."""
    get_config_path().parent.mkdir(parents=True)
    get_config_path().write_text("nico:\n  client_secret: leaked\n")
    get_secrets_path().write_text("nico:\n  client_secret: super-secret-value\n")

    result = runner.invoke(app, ["unset", "--all"], input="y\n")
    assert result.exit_code == 0, result.stdout

    assert not get_config_path().exists()
    assert not get_secrets_path().exists()
