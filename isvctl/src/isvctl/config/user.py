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

"""User-level persisted configuration for isvctl.

Lets users persist the env vars an `isvctl test run` needs once, instead of
re-exporting them in every shell. On disk the values are organized into
provider-namespaced sections (``nico``, ``aws``, ``ngc``, ``isv_lab_service``)
with short keys, e.g. ``nico.api_base`` rather than ``NICO_API_BASE``. They are
split across two files so secrets stay isolated and permission-locked:

- ``config.yml``  (0644) - non-secret values, safe to read aloud or share.
- ``secrets.yml`` (0600) - secret values.

Both live under ``${XDG_CONFIG_HOME:-~/.config}/isvctl/``. ``ISVCTL_CONFIG`` and
``ISVCTL_SECRETS`` override the individual paths (used by tests and advanced
users). The public API is keyed by env var name; the section/key translation
is purely a serialization detail handled here. Loaded values only *fill* env
vars not already present in the process environment, so an explicit ``export``
always wins.

Both files carry a top-level ``version:`` recording the on-disk schema. A file
written by a newer isvctl is rejected with a clear error rather than silently
mis-parsed; a file predating versioning (no ``version:``) is read as version 1.
"""

import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from isvctl.config.env_catalog import (
    ENV_VARS,
    SECTIONS,
    env_to_section_key,
    is_known_section,
    section_key_to_env,
)
from isvctl.redaction import is_secret_env_var

# Only persistable vars may live in the files. Per-run toggles (the "Flags"
# group) are known to the catalog but rejected here — they belong on the CLI or
# an explicit export each time.
_PERSISTABLE_NAMES: frozenset[str] = frozenset(var.name for var in ENV_VARS if var.persistable)
_NON_PERSISTABLE_NAMES: frozenset[str] = frozenset(var.name for var in ENV_VARS if not var.persistable)


def _check_persistable(name: str, where: str) -> None:
    """Raise a clear error if ``name`` is unknown or a non-persistable flag."""
    if name in _NON_PERSISTABLE_NAMES:
        raise ValueError(
            f"{where}'{name}' is a per-run flag and is not persisted; pass it on the command line or export it"
        )
    if name not in _PERSISTABLE_NAMES:
        raise ValueError(f"{where}unknown env var '{name}' (not in the isvctl catalog)")


_FILE_HEADER = (
    "# Managed by `isvctl configure`. Values are grouped into provider sections\n"
    "# (e.g. `nico.api_base`). `version` is the on-disk schema version - leave it\n"
    "# as-is. Edit by hand or re-run `isvctl configure`.\n"
)

# On-disk schema version for config.yml / secrets.yml. Bump only on a breaking
# change to the file layout (section/key renames, a different secret split, ...).
# A file with no `version:` predates versioning and is read as this initial 1.
SCHEMA_VERSION = 1
_VERSION_KEY = "version"

_DIR_MODE = 0o700
_CONFIG_MODE = 0o644
_SECRETS_MODE = 0o600


@dataclass(frozen=True)
class WriteResult:
    """Outcome of persisting user config to disk (the files each value landed in)."""

    config_path: Path
    secrets_path: Path


def _base_dir() -> Path:
    """Return the isvctl config directory, honoring XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "isvctl"


def get_config_path() -> Path:
    """Path to the non-secret config file (``ISVCTL_CONFIG`` overrides)."""
    override = os.environ.get("ISVCTL_CONFIG")
    return Path(override) if override else _base_dir() / "config.yml"


def get_secrets_path() -> Path:
    """Path to the secret config file (``ISVCTL_SECRETS`` overrides)."""
    override = os.environ.get("ISVCTL_SECRETS")
    return Path(override) if override else _base_dir() / "secrets.yml"


def _check_distinct_paths(config_path: Path, secrets_path: Path) -> None:
    """Raise if config.yml and secrets.yml resolve to the same file."""
    config_resolved = config_path.resolve()
    secrets_resolved = secrets_path.resolve()
    if config_resolved == secrets_resolved:
        raise ValueError(f"config and secrets paths must be different (both resolve to {config_resolved})")


def _user_config_paths(config_path: Path | None, secrets_path: Path | None) -> tuple[Path, Path]:
    """Return validated config/secrets paths for a read or write operation."""
    resolved_config_path = config_path or get_config_path()
    resolved_secrets_path = secrets_path or get_secrets_path()
    _check_distinct_paths(resolved_config_path, resolved_secrets_path)
    return resolved_config_path, resolved_secrets_path


def _check_schema_version(path: Path, version: object) -> None:
    """Validate the on-disk schema ``version`` of a single config file.

    A missing version (``None``) is treated as the initial schema - the files
    written before versioning - and accepted. A version newer than this build
    understands is rejected rather than silently mis-parsed against a layout it
    was not written for.

    Raises:
        ValueError: If ``version`` is not a positive integer, or is newer than
            ``SCHEMA_VERSION``.
    """
    if version is None:
        return
    # bool is an int subclass; `version: true` must not slip through.
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{path}: '{_VERSION_KEY}' must be an integer")
    if version < 1:
        raise ValueError(f"{path}: invalid schema version {version}")
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema version {version} was written by a newer isvctl "
            f"(this build supports up to {SCHEMA_VERSION}); upgrade isvctl"
        )


def _read_env_file(path: Path, *, secret_file: bool) -> dict[str, str]:
    """Read and validate one section-organized config file.

    Translates ``section.key`` entries back to env var names.

    Args:
        path: File to read. A missing file yields ``{}``.
        secret_file: True for ``secrets.yml`` (must hold only secret values),
            False for ``config.yml`` (must hold only non-secret values).

    Returns:
        A flat ``{ENV_NAME: value}`` mapping.

    Raises:
        ValueError: On malformed structure, unknown sections/keys, values in
            the wrong file, or non-string values.
    """
    if not path.exists():
        return {}

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping of sections at the top level")

    # Pop the schema version before the section loop so it is not mistaken for
    # an (unknown) section.
    _check_schema_version(path, raw.pop(_VERSION_KEY, None))

    result: dict[str, str] = {}
    for section_name, body in raw.items():
        if not is_known_section(section_name):
            raise ValueError(f"{path}: unknown section '{section_name}'")
        if body is None:
            continue
        if not isinstance(body, dict):
            raise ValueError(f"{path}: section '{section_name}' must be a mapping of key: value")
        for key, value in body.items():
            env_name = section_key_to_env(section_name, key)
            if env_name is None:
                raise ValueError(f"{path}: unknown key '{section_name}.{key}'")
            _check_persistable(env_name, f"{path}: '{section_name}.{key}' maps to ")
            if not isinstance(value, str):
                raise ValueError(f"{path}: '{section_name}.{key}' value must be a string (quote it in YAML)")
            if secret_file and not is_secret_env_var(env_name):
                raise ValueError(f"{path}: '{section_name}.{key}' is not a secret; it belongs in config.yml")
            if not secret_file and is_secret_env_var(env_name):
                raise ValueError(f"{path}: '{section_name}.{key}' is a secret; it belongs in secrets.yml")
            result[env_name] = value
    return result


def load_user_env(config_path: Path | None = None, secrets_path: Path | None = None) -> dict[str, str]:
    """Load and merge persisted env vars from both files.

    Returns:
        A ``{name: value}`` mapping. Secret values override nothing — the two
        files hold disjoint keys by construction.
    """
    config_path, secrets_path = _user_config_paths(config_path, secrets_path)
    config = _read_env_file(config_path, secret_file=False)
    secrets = _read_env_file(secrets_path, secret_file=True)
    return {**config, **secrets}


def apply_user_env(env: dict[str, str]) -> list[str]:
    """Apply loaded env vars to ``os.environ`` without clobbering exports.

    A var already present in the process environment (even set-but-empty) is
    left untouched, preserving the precedence: process env > files.

    Returns:
        The names actually applied (for debug logging — never the values).
    """
    applied: list[str] = []
    for name, value in env.items():
        if name not in os.environ:
            os.environ[name] = value
            applied.append(name)
    return applied


def _to_sections(env: dict[str, str]) -> dict[str, dict[str, str]]:
    """Group a flat ``{ENV_NAME: value}`` map into ordered ``section: {key: value}``."""
    grouped: dict[str, dict[str, str]] = {}
    for name, value in env.items():
        section, key = env_to_section_key(name)
        grouped.setdefault(section, {})[key] = value
    # Stable output: sections in catalog order, keys sorted within each.
    ordered: dict[str, dict[str, str]] = {}
    for section in SECTIONS:
        if section.name in grouped:
            ordered[section.name] = dict(sorted(grouped[section.name].items()))
    return ordered


def _write_all(fd: int, data: bytes) -> None:
    """Write all bytes to ``fd`` or raise on short/failed writes."""
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written == 0:
            raise OSError("short write")
        remaining = remaining[written:]


def _write_env_file(path: Path, env: dict[str, str], mode: int) -> None:
    """Write a section-organized config file with the given mode (0700 dir).

    The parent directory is tightened to 0700 only when we create it. A
    pre-existing directory is left as-is — it may be one the user pointed us at
    via ``ISVCTL_CONFIG``/``ISVCTL_SECRETS`` (``$HOME``, ``/tmp``, a shared
    dir), and silently re-permissioning it would break unrelated files. The
    per-file ``mode`` (0600 for secrets) protects the file regardless.
    """
    parent = path.parent
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(parent, _DIR_MODE)
    if path.is_symlink():
        raise OSError(f"refusing to write through symlink: {path}")

    # `version` first, then the provider sections; sort_keys=False keeps order.
    data: dict[str, object] = {_VERSION_KEY: SCHEMA_VERSION, **_to_sections(env)}
    body = _FILE_HEADER + yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        try:
            _write_all(fd, body.encode("utf-8"))
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, path)
    except BaseException:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass
        raise


def _write_or_remove_env_file(path: Path, env: dict[str, str], mode: int) -> None:
    """Write ``env`` to ``path``, or remove the file when no values remain."""
    if env:
        _write_env_file(path, env, mode)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def write_user_config(
    values: dict[str, str],
    config_path: Path | None = None,
    secrets_path: Path | None = None,
    *,
    existing: dict[str, str] | None = None,
) -> WriteResult:
    """Persist env vars, routing each to config.yml or secrets.yml.

    New values are merged over whatever the files already hold, so a
    provider-scoped run never wipes other providers' values. A file is
    (re)written only when it actually gains a value, so configuring non-secrets
    never rewrites secrets.yml (and vice versa).

    Args:
        existing: Already-loaded current values (as returned by
            ``load_user_env``) for the same paths, supplied to avoid re-reading
            both files. Defaults to reading them from disk.

    Raises:
        ValueError: If a value is not a string or names an unknown env var.
    """
    config_path, secrets_path = _user_config_paths(config_path, secrets_path)

    new_config: dict[str, str] = {}
    new_secrets: dict[str, str] = {}
    for name, value in values.items():
        _check_persistable(name, "")
        if not isinstance(value, str):
            raise ValueError(f"'{name}' value must be a string")
        if is_secret_env_var(name):
            new_secrets[name] = value
        else:
            new_config[name] = value

    if existing is None:
        existing = load_user_env(config_path, secrets_path)
    current_config = {k: v for k, v in existing.items() if not is_secret_env_var(k)}
    current_secrets = {k: v for k, v in existing.items() if is_secret_env_var(k)}

    if new_config:
        _write_env_file(config_path, {**current_config, **new_config}, _CONFIG_MODE)
    if new_secrets:
        _write_env_file(secrets_path, {**current_secrets, **new_secrets}, _SECRETS_MODE)

    return WriteResult(config_path=config_path, secrets_path=secrets_path)


def unset_user_config(
    names: Iterable[str],
    config_path: Path | None = None,
    secrets_path: Path | None = None,
    *,
    existing: dict[str, str] | None = None,
) -> WriteResult:
    """Remove persisted env vars by name while preserving unrelated values.

    Args:
        names: Env var names to remove from config.yml/secrets.yml.
        existing: Already-loaded current values for the same paths, supplied to
            avoid re-reading both files. Defaults to reading them from disk.

    Raises:
        ValueError: If any name is unknown or non-persistable.
    """
    config_path, secrets_path = _user_config_paths(config_path, secrets_path)

    remove_names = set(names)
    for name in remove_names:
        _check_persistable(name, "")

    if existing is None:
        existing = load_user_env(config_path, secrets_path)

    current_config = {k: v for k, v in existing.items() if not is_secret_env_var(k)}
    current_secrets = {k: v for k, v in existing.items() if is_secret_env_var(k)}
    remaining_config = {k: v for k, v in current_config.items() if k not in remove_names}
    remaining_secrets = {k: v for k, v in current_secrets.items() if k not in remove_names}

    if remaining_config != current_config:
        _write_or_remove_env_file(config_path, remaining_config, _CONFIG_MODE)
    if remaining_secrets != current_secrets:
        _write_or_remove_env_file(secrets_path, remaining_secrets, _SECRETS_MODE)

    return WriteResult(config_path=config_path, secrets_path=secrets_path)


def clear_user_config(config_path: Path | None = None, secrets_path: Path | None = None) -> WriteResult:
    """Remove both persisted config files, if present."""
    config_path, secrets_path = _user_config_paths(config_path, secrets_path)
    for path in (config_path, secrets_path):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
    return WriteResult(config_path=config_path, secrets_path=secrets_path)


def file_mode(path: Path) -> int:
    """Return the file's permission bits (e.g. 0o600), for diagnostics."""
    return stat.S_IMODE(path.stat().st_mode)
