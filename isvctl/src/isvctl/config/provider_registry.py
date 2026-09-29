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
from typing import Any

import jsonschema
import yaml

from isvctl.config.suite_resolution import CONFIGS_ROOT, EXTERNAL_PROVIDERS_DIRNAME

SCHEMA_PATH = CONFIGS_ROOT.parent / "schemas" / "provider-registry.schema.json"
REGISTRY_DIRNAME = "providers-registry"


class ProviderRegistryError(Exception):
    """Raised when one or more registry entries are invalid."""


@dataclass(frozen=True)
class Maintainer:
    """A partner contact for a registry entry."""

    github: str
    email: str | None


@dataclass(frozen=True)
class RegistryEntry:
    """One externally maintained provider pinned by the registry."""

    name: str
    vendor: str
    description: str
    repo_url: str
    commit: str
    tested_with: str
    suites: tuple[str, ...]
    maintainers: tuple[Maintainer, ...]
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
    if (configs_root / "providers" / data["name"]).exists():
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
        description=data["description"],
        repo_url=data["repo_url"],
        commit=data["commit"],
        tested_with=data["tested_with"],
        suites=tuple(data["suites"]),
        maintainers=tuple(Maintainer(github=m["github"], email=m.get("email")) for m in data["maintainers"]),
        documentation_url=data["documentation_url"],
        status=data["status"],
    )


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
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems[path.name] = [f"invalid YAML: {exc}"]
            continue
        errors = _entry_errors(path, data, configs_root)
        if errors:
            problems[path.name] = errors
        else:
            entries.append(_to_entry(data))
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


def run_git(*args: str, cwd: Path) -> str:
    """Run a git command and return its stripped stdout.

    Raises:
        RuntimeError: If git is missing or the command fails.
    """
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("git command not found in PATH") from exc
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


# Written inside .git/ so it never shows in `git status`. Only checkouts carrying it
# may be replaced or removed: anything else in providers-external/ is someone's work,
# such as a scaffold under development.
FETCH_MARKER = "isvctl-fetched"


def external_checkout(name: str, configs_root: Path = CONFIGS_ROOT) -> Path:
    """Return where ``isvctl provider fetch <name>`` and ``isvctl provider scaffold <name>`` put a provider."""
    return configs_root / EXTERNAL_PROVIDERS_DIRNAME / name


def is_fetched_checkout(path: Path) -> bool:
    """Return True if ``path`` was created by ``isvctl provider fetch``."""
    return (path / ".git" / FETCH_MARKER).is_file()


def mark_fetched(path: Path) -> None:
    """Record that ``path`` was created by ``isvctl provider fetch``."""
    (path / ".git" / FETCH_MARKER).write_text("created by isvctl provider fetch\n", encoding="utf-8")


def fetched_commit(name: str, configs_root: Path = CONFIGS_ROOT) -> str | None:
    """Return the commit a registered provider is fetched at, or None if there is no fetched checkout."""
    checkout = external_checkout(name, configs_root)
    if not is_fetched_checkout(checkout):
        return None
    try:
        return run_git("rev-parse", "HEAD", cwd=checkout)
    except RuntimeError:
        return None


def ensure_fetched(provider: str, configs_root: Path = CONFIGS_ROOT) -> RegistryEntry | None:
    """Return the registry entry for ``provider`` once it is fetched at its pinned commit.

    In-tree providers and names the registry does not know return None, so the
    caller's usual unknown-provider handling still applies. A local directory
    that ``fetch`` did not create, such as the partner's own scaffold under
    development, is run as it is.

    Invalid registry entries are skipped, so an unfinished entry for this very
    provider leaves its local scaffold runnable.

    Raises:
        ProviderRegistryError: If the provider is not fetched, or is fetched at a
            different commit than the registry pins.
    """
    if (configs_root / "providers" / provider).is_dir():
        return None
    entries, _ = load_registry_skipping_invalid(configs_root)
    entry = next((entry for entry in entries if entry.name == provider), None)
    if entry is None:
        return None
    checkout = external_checkout(provider, configs_root)
    if checkout.is_dir() and not is_fetched_checkout(checkout):
        return entry
    head = fetched_commit(provider, configs_root)
    hint = f"Run: isvctl provider fetch {provider}"
    if head is None:
        raise ProviderRegistryError(f"Provider '{provider}' is registered but not fetched. {hint}")
    if head != entry.commit:
        raise ProviderRegistryError(
            f"Provider '{provider}' is fetched at {head[:12]}, but the registry pins {entry.commit[:12]}. {hint}"
        )
    return entry
