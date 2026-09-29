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
REGISTRY_DIRNAME = "provider-registry"


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
    commit: str | None
    ref: str | None
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
    """Render a schema error as ``<field>: <message>``.

    A failed ``anyOf`` of ``required`` alternatives would otherwise print the
    whole entry, so it is rewritten to name the fields instead.
    """
    location = "/".join(str(part) for part in error.absolute_path) or "entry"
    alternatives = error.validator_value if error.validator == "anyOf" else []
    if alternatives and all("required" in alt for alt in alternatives):
        fields = " or ".join(f"'{field}'" for alt in alternatives for field in alt["required"])
        return f"{location}: must set {fields}"
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
        commit=data.get("commit"),
        ref=data.get("ref"),
        tested_with=data["tested_with"],
        suites=tuple(data["suites"]),
        maintainers=tuple(Maintainer(github=m["github"], email=m.get("email")) for m in data["maintainers"]),
        documentation_url=data["documentation_url"],
        status=data["status"],
    )


def load_registry(configs_root: Path = CONFIGS_ROOT) -> list[RegistryEntry]:
    """Load every ``provider-registry/*.yaml`` entry, sorted by name.

    Raises:
        ProviderRegistryError: If any entry is invalid. The message lists the
            problems of every file; an entry's filename and suite checks run
            only once it passes the schema.
    """
    registry_dir = configs_root / REGISTRY_DIRNAME
    entries = []
    problems = []
    for path in sorted(registry_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: invalid YAML: {exc}")
            continue
        errors = _entry_errors(path, data, configs_root)
        if errors:
            problems.extend(f"{path.name}: {error}" for error in errors)
        else:
            entries.append(_to_entry(data))
    if problems:
        raise ProviderRegistryError("Invalid provider registry entries:\n" + "\n".join(f"  {p}" for p in problems))
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


def external_checkout(name: str, configs_root: Path = CONFIGS_ROOT) -> Path:
    """Return where ``isvctl provider fetch <name>`` checks a registered provider out."""
    return configs_root / EXTERNAL_PROVIDERS_DIRNAME / name


def fetched_commit(name: str, configs_root: Path = CONFIGS_ROOT) -> str | None:
    """Return the commit a registered provider is fetched at, or None if it is not fetched."""
    checkout = external_checkout(name, configs_root)
    if not (checkout / ".git").is_dir():
        return None
    try:
        return run_git("rev-parse", "HEAD", cwd=checkout)
    except RuntimeError:
        return None


def ensure_fetched(provider: str, configs_root: Path = CONFIGS_ROOT) -> None:
    """Fail if ``provider`` is a registered provider that is not fetched at its pinned commit.

    In-tree providers and names the registry does not know pass through, so the
    caller's usual unknown-provider handling still applies.

    Raises:
        ProviderRegistryError: If the provider is not fetched, is fetched at a
            different commit than the registry pins, or the registry is invalid.
    """
    if (configs_root / "providers" / provider).is_dir():
        return
    entry = next((entry for entry in load_registry(configs_root) if entry.name == provider), None)
    if entry is None:
        return
    head = fetched_commit(provider, configs_root)
    hint = f"Run: isvctl provider fetch {provider}"
    if head is None:
        raise ProviderRegistryError(f"Provider '{provider}' is registered but not fetched. {hint}")
    if entry.commit is not None and head != entry.commit:
        raise ProviderRegistryError(
            f"Provider '{provider}' is fetched at {head[:12]}, but the registry pins {entry.commit[:12]}. {hint}"
        )
