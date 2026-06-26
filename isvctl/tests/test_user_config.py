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

"""Tests for user-level persisted config (config.yml / secrets.yml)."""

import os
from pathlib import Path

import pytest
import yaml

from isvctl.config.user import (
    SCHEMA_VERSION,
    apply_user_env,
    clear_user_config,
    file_mode,
    get_config_path,
    get_secrets_path,
    load_user_env,
    unset_user_config,
    write_user_config,
)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point config + secrets at a temp dir and clear path overrides."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ISVCTL_CONFIG", raising=False)
    monkeypatch.delenv("ISVCTL_SECRETS", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_default_paths_honor_xdg(isolated_env: Path) -> None:
    """Default config/secrets paths live under XDG_CONFIG_HOME/isvctl."""
    assert get_config_path() == isolated_env / "isvctl" / "config.yml"
    assert get_secrets_path() == isolated_env / "isvctl" / "secrets.yml"


def test_path_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISVCTL_CONFIG/ISVCTL_SECRETS override the individual file paths."""
    monkeypatch.setenv("ISVCTL_CONFIG", "/tmp/c.yml")
    monkeypatch.setenv("ISVCTL_SECRETS", "/tmp/s.yml")
    assert get_config_path() == Path("/tmp/c.yml")
    assert get_secrets_path() == Path("/tmp/s.yml")


@pytest.mark.parametrize("operation", ["load", "write", "unset", "clear"])
def test_user_config_operations_reject_identical_paths(isolated_env: Path, operation: str) -> None:
    """User config helpers reject config and secrets paths resolving to one file."""
    same_path = isolated_env / "same.yml"
    paths = {"config_path": same_path, "secrets_path": same_path}

    with pytest.raises(ValueError, match="must be different"):
        if operation == "load":
            load_user_env(**paths)
        elif operation == "write":
            write_user_config({"NICO_API_BASE": "https://x"}, **paths)
        elif operation == "unset":
            unset_user_config(["NICO_API_BASE"], **paths)
        else:
            clear_user_config(**paths)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_missing_files_return_empty(isolated_env: Path) -> None:
    """Loading with no files present yields an empty mapping."""
    assert load_user_env() == {}


def test_merges_both_files(isolated_env: Path) -> None:
    """load_user_env merges values from config.yml and secrets.yml."""
    config = get_config_path()
    secrets = get_secrets_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  api_base: https://nico.example.com\n")
    secrets.write_text("nico:\n  client_secret: shhh\n")

    env = load_user_env()
    assert env == {
        "NICO_API_BASE": "https://nico.example.com",
        "NICO_CLIENT_SECRET": "shhh",
    }


def test_secret_in_config_file_raises(isolated_env: Path) -> None:
    """A secret stored in config.yml is rejected on load."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  client_secret: shhh\n")
    with pytest.raises(ValueError, match="belongs in secrets"):
        load_user_env()


def test_non_secret_in_secrets_file_raises(isolated_env: Path) -> None:
    """A non-secret stored in secrets.yml is rejected on load."""
    secrets = get_secrets_path()
    secrets.parent.mkdir(parents=True)
    secrets.write_text("nico:\n  api_base: https://x\n")
    with pytest.raises(ValueError, match="belongs in config"):
        load_user_env()


def test_unknown_section_raises(isolated_env: Path) -> None:
    """An unrecognized top-level section is rejected on load."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("bogus:\n  x: 1\n")
    with pytest.raises(ValueError, match="unknown section"):
        load_user_env()


def test_unknown_key_raises(isolated_env: Path) -> None:
    """An unrecognized key within a known section is rejected on load."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  totally_made_up: 1\n")
    with pytest.raises(ValueError, match="unknown key"):
        load_user_env()


def test_write_rejects_flag_var(isolated_env: Path) -> None:
    """Persisting a per-run flag var is rejected."""
    with pytest.raises(ValueError, match="per-run flag"):
        write_user_config({"AWS_SKIP_TEARDOWN": "1"})


def test_non_string_value_raises(isolated_env: Path) -> None:
    """A non-string value in a config file is rejected on load."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  site_id: 12345\n")
    with pytest.raises(ValueError, match="must be a string"):
        load_user_env()


def test_load_rejects_non_persistable_catalog_var(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolved non-persistable catalog var is rejected on load."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  api_base: kubectl\n")
    monkeypatch.setattr("isvctl.config.user.section_key_to_env", lambda _section, _key: "KUBECTL")

    with pytest.raises(ValueError, match="per-run flag"):
        load_user_env()


# ---------------------------------------------------------------------------
# apply_user_env
# ---------------------------------------------------------------------------


def test_apply_sets_absent_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_user_env sets a var that isn't already in the environment."""
    monkeypatch.delenv("NICO_API_BASE", raising=False)
    applied = apply_user_env({"NICO_API_BASE": "https://x"})
    assert applied == ["NICO_API_BASE"]
    assert os.environ["NICO_API_BASE"] == "https://x"


def test_apply_never_overwrites_exported_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_user_env never clobbers an already-exported var."""
    monkeypatch.setenv("NICO_API_BASE", "from-shell")
    applied = apply_user_env({"NICO_API_BASE": "from-file"})
    assert applied == []
    assert os.environ["NICO_API_BASE"] == "from-shell"


def test_apply_preserves_set_but_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A set-but-empty exported var counts as present and is preserved."""
    monkeypatch.setenv("NICO_API_BASE", "")
    applied = apply_user_env({"NICO_API_BASE": "from-file"})
    assert applied == []
    assert os.environ["NICO_API_BASE"] == ""


# ---------------------------------------------------------------------------
# write_user_config
# ---------------------------------------------------------------------------


def test_write_routes_by_secret_ness(isolated_env: Path) -> None:
    """Secrets land in secrets.yml and non-secrets in config.yml."""
    result = write_user_config(
        {
            "NICO_API_BASE": "https://nico.example.com",
            "NICO_CLIENT_SECRET": "shhh",
        }
    )
    assert result.config_path.exists() and result.secrets_path.exists()

    config_data = yaml.safe_load(get_config_path().read_text())
    secrets_data = yaml.safe_load(get_secrets_path().read_text())
    assert config_data == {"version": SCHEMA_VERSION, "nico": {"api_base": "https://nico.example.com"}}
    assert secrets_data == {"version": SCHEMA_VERSION, "nico": {"client_secret": "shhh"}}


def test_secrets_file_is_0600(isolated_env: Path) -> None:
    """secrets.yml is written with 0600 permissions."""
    write_user_config({"NICO_CLIENT_SECRET": "shhh"})
    assert file_mode(get_secrets_path()) == 0o600


def test_config_file_is_0644(isolated_env: Path) -> None:
    """config.yml is written with 0644 permissions."""
    write_user_config({"NICO_API_BASE": "https://x"})
    assert file_mode(get_config_path()) == 0o644


def test_write_merges_with_existing(isolated_env: Path) -> None:
    """A second write merges over existing values without wiping others."""
    write_user_config({"NICO_API_BASE": "https://x", "AWS_REGION": "us-west-2"})
    # A provider-scoped second write must not wipe AWS_REGION.
    write_user_config({"NICO_API_BASE": "https://y"})

    env = load_user_env()
    assert env["NICO_API_BASE"] == "https://y"
    assert env["AWS_REGION"] == "us-west-2"


def test_write_roundtrips_through_load(isolated_env: Path) -> None:
    """A written value reads back identically through load_user_env."""
    write_user_config({"NICO_SITE_ID": "00000000-0000-0000-0000-000000000000"})
    assert load_user_env() == {"NICO_SITE_ID": "00000000-0000-0000-0000-000000000000"}


def test_unset_removes_value_and_preserves_other_files(isolated_env: Path) -> None:
    """unset_user_config removes selected values without touching unrelated files."""
    write_user_config(
        {
            "NICO_API_BASE": "https://nico.example.com",
            "AWS_REGION": "us-west-2",
            "NICO_CLIENT_SECRET": "shhh",
        }
    )
    secrets_before = get_secrets_path().read_bytes()

    unset_user_config(["NICO_API_BASE"])

    assert load_user_env() == {"AWS_REGION": "us-west-2", "NICO_CLIENT_SECRET": "shhh"}
    assert get_secrets_path().read_bytes() == secrets_before


def test_unset_deletes_file_when_last_value_is_removed(isolated_env: Path) -> None:
    """Removing the last value in a file deletes that empty config file."""
    write_user_config({"NICO_API_BASE": "https://nico.example.com"})

    unset_user_config(["NICO_API_BASE"])

    assert not get_config_path().exists()
    assert load_user_env() == {}


def test_unset_rejects_unknown_name(isolated_env: Path) -> None:
    """unset_user_config validates names against the same catalog as writes."""
    with pytest.raises(ValueError, match="unknown env var"):
        unset_user_config(["NOT_A_REAL_ENV"])


def test_clear_user_config_removes_both_files(isolated_env: Path) -> None:
    """clear_user_config removes all persisted values and both backing files."""
    write_user_config({"NICO_API_BASE": "https://nico.example.com", "NICO_CLIENT_SECRET": "shhh"})

    clear_user_config()

    assert not get_config_path().exists()
    assert not get_secrets_path().exists()
    assert load_user_env() == {}


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


def test_write_stamps_version_into_both_files(isolated_env: Path) -> None:
    """Both files are stamped with the current schema version on write."""
    write_user_config({"NICO_API_BASE": "https://x", "NICO_CLIENT_SECRET": "shhh"})
    config_data = yaml.safe_load(get_config_path().read_text())
    secrets_data = yaml.safe_load(get_secrets_path().read_text())
    assert config_data["version"] == SCHEMA_VERSION
    assert secrets_data["version"] == SCHEMA_VERSION


def test_version_is_first_line(isolated_env: Path) -> None:
    """The version key is rendered above the provider sections."""
    # Stable, human-friendly ordering: the version sits above the sections.
    write_user_config({"NICO_API_BASE": "https://x"})
    lines = [ln for ln in get_config_path().read_text().splitlines() if not ln.startswith("#")]
    assert lines[0] == f"version: {SCHEMA_VERSION}"


def test_versioned_file_roundtrips(isolated_env: Path) -> None:
    """A versioned file written by isvctl reads back identically."""
    write_user_config({"NICO_API_BASE": "https://x", "NICO_CLIENT_SECRET": "shhh"})
    assert load_user_env() == {"NICO_API_BASE": "https://x", "NICO_CLIENT_SECRET": "shhh"}


def test_missing_version_reads_as_initial_schema(isolated_env: Path) -> None:
    """A file without a version key loads as the initial schema."""
    # Files written before versioning have no `version:` and must still load.
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("nico:\n  api_base: https://x\n")
    assert load_user_env() == {"NICO_API_BASE": "https://x"}


# ---------------------------------------------------------------------------
# Directory permissions
# ---------------------------------------------------------------------------


def test_creates_default_dir_as_0700(isolated_env: Path) -> None:
    """A config dir created by isvctl is locked down to 0700."""
    # The directory we create gets locked down to owner-only.
    write_user_config({"NICO_API_BASE": "https://x"})
    assert file_mode(get_config_path().parent) == 0o700


def test_writing_non_secret_does_not_touch_secrets_file(isolated_env: Path) -> None:
    """A non-secret-only write does not create secrets.yml."""
    # A non-secret-only write must not create or rewrite secrets.yml.
    write_user_config({"NICO_API_BASE": "https://x"})
    assert get_config_path().exists()
    assert not get_secrets_path().exists()


def test_writing_secret_only_leaves_existing_config_untouched(isolated_env: Path) -> None:
    """Adding a secret leaves an unchanged config.yml byte-identical."""
    # Adding a secret must not rewrite an unrelated, unchanged config.yml.
    write_user_config({"NICO_API_BASE": "https://x"})
    before = get_config_path().read_bytes()
    write_user_config({"NICO_CLIENT_SECRET": "shhh"})
    assert get_config_path().read_bytes() == before  # config.yml byte-identical
    assert get_secrets_path().exists()


def test_failed_write_leaves_existing_file_intact(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A low-level write failure leaves the previous config.yml bytes intact."""
    write_user_config({"NICO_API_BASE": "https://before.example.com"})
    before = get_config_path().read_bytes()

    def fail_write(fd: int, data: bytes) -> int:
        raise OSError("no space left")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(OSError, match="no space left"):
        write_user_config({"NICO_API_BASE": "https://after.example.com"})

    assert get_config_path().read_bytes() == before


def test_refuses_to_write_through_a_symlink(isolated_env: Path) -> None:
    """Writing refuses to follow a symlink planted at the target path."""
    # O_NOFOLLOW must stop secrets being redirected through a planted symlink.
    secrets = get_secrets_path()
    secrets.parent.mkdir(parents=True)
    target = isolated_env / "victim"
    secrets.symlink_to(target)
    with pytest.raises(OSError):
        write_user_config({"NICO_CLIENT_SECRET": "shhh"})
    assert not target.exists()  # nothing written through the link


def test_does_not_re_permission_existing_override_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A pre-existing override directory keeps its permissions on write."""
    # A pre-existing directory the user points us at via ISVCTL_CONFIG/SECRETS
    # must NOT be silently re-chmod'd to 0700 (it could be $HOME or a shared dir).
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)  # mkdir mode is subject to umask; force it.
    monkeypatch.setenv("ISVCTL_CONFIG", str(shared / "config.yml"))
    monkeypatch.setenv("ISVCTL_SECRETS", str(shared / "secrets.yml"))

    write_user_config({"NICO_API_BASE": "https://x", "NICO_CLIENT_SECRET": "shhh"})

    assert file_mode(shared) == 0o755  # directory perms untouched
    assert file_mode(shared / "secrets.yml") == 0o600  # file still locked down


def test_explicit_current_version_loads(isolated_env: Path) -> None:
    """A file stamped with the current schema version loads normally."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(f"version: {SCHEMA_VERSION}\nnico:\n  api_base: https://x\n")
    assert load_user_env() == {"NICO_API_BASE": "https://x"}


def test_newer_version_raises(isolated_env: Path) -> None:
    """A config.yml from a newer schema version is rejected."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(f"version: {SCHEMA_VERSION + 1}\nnico:\n  api_base: https://x\n")
    with pytest.raises(ValueError, match="newer isvctl"):
        load_user_env()


def test_newer_version_in_secrets_raises(isolated_env: Path) -> None:
    """A secrets.yml from a newer schema version is rejected."""
    secrets = get_secrets_path()
    secrets.parent.mkdir(parents=True)
    secrets.write_text(f"version: {SCHEMA_VERSION + 1}\nnico:\n  client_secret: shhh\n")
    with pytest.raises(ValueError, match="newer isvctl"):
        load_user_env()


@pytest.mark.parametrize("bad", ['"1"', "true", "1.0", "[]"])
def test_non_integer_version_raises(isolated_env: Path, bad: str) -> None:
    """A non-integer version value is rejected on load."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(f"version: {bad}\nnico:\n  api_base: https://x\n")
    with pytest.raises(ValueError, match="must be an integer"):
        load_user_env()


def test_zero_version_raises(isolated_env: Path) -> None:
    """A version below the initial schema (0) is rejected on load."""
    config = get_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("version: 0\nnico:\n  api_base: https://x\n")
    with pytest.raises(ValueError, match="invalid schema version"):
        load_user_env()
