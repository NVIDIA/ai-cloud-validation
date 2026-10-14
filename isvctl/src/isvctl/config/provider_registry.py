# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Load and validate the registry of externally maintained providers, and inspect their fetched copies."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from isvtest.core.process import run_command_process

from isvctl.config.suite_resolution import (
    CONFIGS_ROOT,
    external_provider_dir,
    in_tree_provider_dir,
    is_fetched_checkout,
    provider_dir,
)

SCHEMA_PATH = CONFIGS_ROOT.parent / "schemas" / "provider-registry.schema.json"
REGISTRY_DIRNAME = "providers-registry"

FetchState = Literal["missing", "local", "stale", "current"]


class ProviderRegistryError(Exception):
    """Raised when one or more registry entries are invalid."""


@dataclass(frozen=True)
class RegistryEntry:
    """One externally maintained provider pinned by the registry."""

    name: str
    vendor: str
    repo_url: str
    path: str | None
    commit: str
    tested_with: str
    suites: tuple[str, ...]
    documentation_url: str
    status: str


@cache
def _validator() -> jsonschema.Draft202012Validator:
    """Return a validator for the registry entry schema."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _format_error(error: jsonschema.ValidationError) -> str:
    """Render a schema error as ``<field>: <message>``."""
    location = "/".join(str(part) for part in error.absolute_path) or "entry"
    return f"{location}: {error.message}"


def _entry_errors(path: Path, data: Any, configs_root: Path) -> list[str]:
    """Return every problem with one registry file's content."""
    schema_errors = sorted(_validator().iter_errors(data), key=lambda e: [str(part) for part in e.absolute_path])
    if schema_errors:
        return [_format_error(error) for error in schema_errors]

    errors = []
    if data["name"] != path.stem:
        errors.append(f"name: '{data['name']}' must match the filename '{path.stem}'")
    if in_tree_provider_dir(data["name"], configs_root).exists():
        errors.append(f"name: '{data['name']}' is already an in-tree provider in providers/")
    suites_dir = configs_root / "suites"
    errors.extend(
        f"suites: '{suite}' is not a suite in {suites_dir.name}/"
        for suite in data["suites"]
        if not (suites_dir / f"{suite}.yaml").is_file()
    )
    return errors


def _to_entry(data: dict[str, Any]) -> RegistryEntry:
    """Build a registry entry from schema-valid data."""
    return RegistryEntry(
        name=data["name"],
        vendor=data["vendor"],
        repo_url=data["repo_url"],
        path=data.get("path"),
        commit=data["commit"],
        tested_with=data["tested_with"],
        suites=tuple(data["suites"]),
        documentation_url=data["documentation_url"],
        status=data["status"],
    )


def registry_entry_path(name: str, configs_root: Path = CONFIGS_ROOT) -> Path:
    """Return where the registry entry for provider ``name`` lives; the path may not exist."""
    return configs_root / REGISTRY_DIRNAME / f"{name}.yaml"


class _RegistryLoader(yaml.SafeLoader):
    """Reject ambiguous registry mappings instead of silently overwriting duplicate keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """Construct a safe mapping and require one distinct key per entry."""
        mapping = super().construct_mapping(node, deep=deep)
        if len(mapping) != len(node.value):
            raise yaml.constructor.ConstructorError(
                "while constructing a registry mapping", node.start_mark, "duplicate mapping key", node.start_mark
            )
        return mapping


def _load_entry_file(path: Path, configs_root: Path) -> tuple[RegistryEntry | None, list[str]]:
    """Load one registry file: the entry if it is valid, else None and its problems."""
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_RegistryLoader)
    except yaml.YAMLError as exc:
        return None, [f"invalid YAML: {exc}"]
    errors = _entry_errors(path, data, configs_root)
    return (None, errors) if errors else (_to_entry(data), [])


def load_entry(name: str, configs_root: Path = CONFIGS_ROOT) -> tuple[RegistryEntry | None, list[str]]:
    """Load the registry entry for provider ``name`` alone: the entry if it is valid, else None and its problems.

    A name the registry does not know returns None and no problems.
    """
    path = registry_entry_path(name, configs_root)
    # Keeps a name such as '../x' from reading a file outside the registry.
    if Path(name).name != name or not path.is_file():
        return None, []
    return _load_entry_file(path, configs_root)


def load_registry_skipping_invalid(
    configs_root: Path = CONFIGS_ROOT,
) -> tuple[list[RegistryEntry], dict[str, list[str]]]:
    """Load the valid ``providers-registry/*.yaml`` entries, sorted by name, and the problems of the others.

    For everyday commands: one unfinished entry, such as a stub that
    ``isvctl provider scaffold`` wrote, must not block every other provider.
    The problems are keyed by file name; an entry's filename and suite checks
    run only once it passes the schema.
    """
    registry_dir = configs_root / REGISTRY_DIRNAME
    entries = []
    problems: dict[str, list[str]] = {}
    for path in sorted(registry_dir.glob("*.yaml")):
        entry, errors = _load_entry_file(path, configs_root)
        if entry is None:
            problems[path.name] = errors
        else:
            entries.append(entry)
    return entries, problems


def load_registry(configs_root: Path = CONFIGS_ROOT) -> list[RegistryEntry]:
    """Load every ``providers-registry/*.yaml`` entry, sorted by name, failing on any invalid one.

    Used where the whole registry must be valid: the pre-commit hook and tests.

    Raises:
        ProviderRegistryError: If any entry is invalid. The message lists the
            problems of every file.
    """
    entries, problems = load_registry_skipping_invalid(configs_root)
    if problems:
        lines = [f"  {name}: {error}" for name, errors in problems.items() for error in errors]
        raise ProviderRegistryError("Invalid provider registry entries:\n" + "\n".join(lines))
    return entries


def _git_env() -> dict[str, str]:
    """Return the environment without ``GIT_*`` variables that redirect git to another repository.

    Git exports variables such as ``GIT_DIR`` to hooks, so an isvctl run from a hook
    would otherwise fetch into the caller's repository. Transport, auth and ``-c``-style
    config variables are kept (the same allowlist as pre-commit's ``no_git_env``).
    """
    keep = {
        "GIT_EXEC_PATH",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_NO_VERIFY",
        "GIT_CONFIG_COUNT",
        "GIT_HTTP_PROXY_AUTHMETHOD",
        "GIT_ALLOW_PROTOCOL",
        "GIT_ASKPASS",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in keep or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }


def run_git(*args: str, cwd: Path, timeout: float = 600) -> str:
    """Run a git command and return its stripped stdout.

    On timeout the whole process group is stopped, including the transport (ssh,
    ``git-remote-https``) that ``git fetch`` starts.

    Raises:
        RuntimeError: If git is missing, the command fails, or it runs longer than ``timeout`` seconds.
    """
    try:
        result = run_command_process(["git", *args], cwd=cwd, env=_git_env(), timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("git command not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {args[0]} timed out after {timeout:g}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def fetch_state(entry: RegistryEntry, configs_root: Path = CONFIGS_ROOT) -> tuple[FetchState, str | None]:
    """Classify the checkout of ``entry`` in ``providers-external/`` and return the commit it is at.

    The state is ``missing`` (no usable fetched checkout), ``local`` (a directory
    that ``fetch`` did not create, such as the partner's own scaffold), ``stale``
    (fetched at another commit or provider path than the registry pins) or ``current``. The
    commit is None unless the checkout was fetched.
    """
    checkout = external_provider_dir(entry.name, configs_root)
    if not is_fetched_checkout(checkout):
        return ("local" if checkout.is_dir() else "missing"), None
    try:
        head = run_git("rev-parse", "HEAD", cwd=checkout)
    except RuntimeError:
        return "missing", None
    path_matches = provider_dir(entry.name, configs_root) == checkout / (entry.path or "")
    return ("current" if head == entry.commit and path_matches else "stale"), head


def ensure_fetched(provider: str, configs_root: Path = CONFIGS_ROOT) -> RegistryEntry | None:
    """Return the registry entry for ``provider`` once its fetched commit and path match.

    In-tree providers and names the registry does not know return None, so the
    caller's usual unknown-provider handling still applies. A local directory
    that ``fetch`` did not create, such as the partner's own scaffold under
    development, also returns None and is run as it is: the entry describes the
    pinned commit, not that directory.

    Invalid registry entries leave local scaffolds runnable, but block fetched
    checkouts so malformed policy cannot disable their registry checks.

    Raises:
        ProviderRegistryError: If the provider is not fetched, or is fetched at a
            different commit or provider path than the registry pins, or its fetched
            checkout has an invalid registry entry.
    """
    if in_tree_provider_dir(provider, configs_root).is_dir():
        return None
    entry, errors = load_entry(provider, configs_root)
    if errors and is_fetched_checkout(external_provider_dir(provider, configs_root)):
        raise ProviderRegistryError(f"Invalid registry entry '{provider}': {'; '.join(errors)}")
    if entry is None:
        return None
    state, head = fetch_state(entry, configs_root)
    if state == "local":
        return None
    hint = f"Run: isvctl provider fetch {provider}"
    if state == "missing":
        raise ProviderRegistryError(f"Provider '{provider}' is registered but not fetched. {hint}")
    if state == "stale":
        if head == entry.commit:
            raise ProviderRegistryError(
                f"Provider '{provider}' is fetched with a different provider path than the registry specifies. {hint}"
            )
        raise ProviderRegistryError(
            f"Provider '{provider}' is fetched at {head[:12]}, but the registry pins {entry.commit[:12]}. {hint}"
        )
    return entry


def version_warning(entry: RegistryEntry, isvctl_version: str) -> str | None:
    """Return a warning if ``entry`` was validated against another isvctl release than ``isvctl_version``."""
    # Entries spell release candidates X.Y.Z-rcN; package metadata spells them X.Y.ZrcN.
    if entry.tested_with.replace("-rc", "rc") == isvctl_version:
        return None
    return (
        f"'{entry.name}' was validated against isvctl {entry.tested_with}, but this is isvctl {isvctl_version}; "
        "its configs may not match this version's suites."
    )
