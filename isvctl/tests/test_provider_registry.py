# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the external provider registry loader and its CLI commands."""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from typer.testing import CliRunner

from isvctl.cli import provider as provider_cli
from isvctl.cli import test as test_cli
from isvctl.config.provider_registry import (
    Maintainer,
    ProviderRegistryError,
    RegistryEntry,
    ensure_fetched,
    load_registry,
    load_registry_skipping_invalid,
    mark_fetched,
)
from isvctl.orchestrator.loop import OrchestratorResult, Phase, PhaseResult

COMMIT = "0123456789abcdef0123456789abcdef01234567"

runner = CliRunner()


def _entry(**overrides: Any) -> dict[str, Any]:
    """Return a valid qualified entry named ``acme``, with fields overridden or removed (``None``)."""
    entry: dict[str, Any] = {
        "schema_version": 1,
        "name": "acme",
        "vendor": "Acme Cloud Inc.",
        "description": "Acme GPU instances.",
        "repo_url": "https://github.com/acme/isvctl-provider-acme",
        "commit": COMMIT,
        "tested_with": "0.13.0",
        "suites": ["vm"],
        "maintainers": [{"github": "acme-handle", "email": "oss@acme.example"}],
        "documentation_url": "https://github.com/acme/isvctl-provider-acme#reproducing",
        "status": "qualified",
    }
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not None}


def _configs_root(tmp_path: Path, entries: dict[str, dict[str, Any]], suites: tuple[str, ...] = ("vm",)) -> Path:
    """Write suites and ``<filename>: entry`` registry files under a temporary configs root."""
    configs_root = tmp_path / "configs"
    (configs_root / "suites").mkdir(parents=True)
    for suite in suites:
        (configs_root / "suites" / f"{suite}.yaml").write_text("tests: {}\n", encoding="utf-8")
    registry_dir = configs_root / "providers-registry"
    registry_dir.mkdir()
    (registry_dir / "README.md").write_text("# Provider Registry\n", encoding="utf-8")
    for filename, entry in entries.items():
        (registry_dir / filename).write_text(yaml.safe_dump(entry), encoding="utf-8")
    return configs_root


def _problems(configs_root: Path) -> str:
    """Return the error message raised for an invalid registry."""
    with pytest.raises(ProviderRegistryError) as excinfo:
        load_registry(configs_root)
    return str(excinfo.value)


def test_loads_valid_entry(tmp_path: Path) -> None:
    """A valid entry loads with every field, and non-YAML files such as README.md are ignored."""
    [entry] = load_registry(_configs_root(tmp_path, {"acme.yaml": _entry()}))

    assert entry.name == "acme"
    assert entry.commit == COMMIT
    assert entry.ref is None
    assert entry.suites == ("vm",)
    assert entry.maintainers == (Maintainer(github="acme-handle", email="oss@acme.example"),)
    assert entry.status == "qualified"


def test_entries_are_sorted_by_name(tmp_path: Path) -> None:
    """Entries come back in name order regardless of creation order."""
    configs_root = _configs_root(tmp_path, {"zeta.yaml": _entry(name="zeta"), "acme.yaml": _entry()})

    assert [entry.name for entry in load_registry(configs_root)] == ["acme", "zeta"]


def test_missing_registry_directory_is_empty(tmp_path: Path) -> None:
    """A configs root without a registry directory has no entries."""
    assert load_registry(tmp_path) == []


def test_experimental_entry_may_pin_only_a_ref(tmp_path: Path) -> None:
    """Experimental entries may track a ref instead of a commit."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry(status="experimental", commit=None, ref="main")})

    [entry] = load_registry(configs_root)

    assert entry.commit is None
    assert entry.ref == "main"


@pytest.mark.parametrize(
    ("schema_version", "expected"),
    [
        (None, "'schema_version' is a required property"),
        (2, "schema_version: 2 is not one of [1]"),
        ("1", "schema_version: '1' is not of type 'integer'"),
    ],
)
def test_requires_known_schema_version(tmp_path: Path, schema_version: int | str | None, expected: str) -> None:
    """Every entry declares a schema version this build understands."""
    problems = _problems(_configs_root(tmp_path, {"acme.yaml": _entry(schema_version=schema_version)}))

    assert expected in problems


def test_rejects_name_that_does_not_match_filename(tmp_path: Path) -> None:
    """The entry name is the fetch key, so it must equal the filename stem."""
    problems = _problems(_configs_root(tmp_path, {"acme.yaml": _entry(name="other")}))

    assert "acme.yaml: name: 'other' must match the filename 'acme'" in problems


def test_qualified_entry_requires_commit(tmp_path: Path) -> None:
    """Only experimental entries may omit the pinned commit."""
    problems = _problems(_configs_root(tmp_path, {"acme.yaml": _entry(commit=None, ref="v1.0.0")}))

    assert "acme.yaml: entry: 'commit' is a required property" in problems


def test_experimental_entry_requires_commit_or_ref(tmp_path: Path) -> None:
    """An entry with neither commit nor ref has nothing to fetch, and the error names both fields."""
    problems = _problems(_configs_root(tmp_path, {"acme.yaml": _entry(status="experimental", commit=None)}))

    assert "acme.yaml: entry: must set 'commit' or 'ref'" in problems


def test_rejects_unknown_suite(tmp_path: Path) -> None:
    """Every declared suite must exist in the suites directory."""
    problems = _problems(_configs_root(tmp_path, {"acme.yaml": _entry(suites=["vm", "nope"])}))

    assert "acme.yaml: suites: 'nope' is not a suite in suites/" in problems


@pytest.mark.parametrize("url", ["http://github.com/acme/x", "file:///tmp/x", "ext::sh -c true"])
def test_rejects_non_https_repo_url(tmp_path: Path, url: str) -> None:
    """Only https URLs reach ``git fetch``, which rules out local paths and git's ext transport."""
    problems = _problems(_configs_root(tmp_path, {"acme.yaml": _entry(repo_url=url)}))

    assert "acme.yaml: repo_url:" in problems


def test_reports_every_problem_across_files(tmp_path: Path) -> None:
    """All invalid files are reported in one error rather than stopping at the first."""
    configs_root = _configs_root(
        tmp_path,
        {
            "acme.yaml": _entry(commit="abc123"),
            "zeta.yaml": _entry(name="zeta", tested_with="v0.13.0"),
        },
    )

    problems = _problems(configs_root)

    assert "acme.yaml: commit:" in problems
    assert "zeta.yaml: tested_with:" in problems


def test_reports_invalid_yaml(tmp_path: Path) -> None:
    """A file that is not parseable YAML is reported by name."""
    configs_root = _configs_root(tmp_path, {})
    (configs_root / "providers-registry" / "acme.yaml").write_text("name: [unclosed\n", encoding="utf-8")

    assert "acme.yaml: invalid YAML:" in _problems(configs_root)


def _list(monkeypatch: pytest.MonkeyPatch, configs_root: Path, *args: str) -> tuple[int, str]:
    """Run ``isvctl provider list`` against a temporary configs root."""
    monkeypatch.setattr(provider_cli, "CONFIGS_ROOT", configs_root)
    result = runner.invoke(provider_cli.app, ["list", *args])
    return result.exit_code, result.output


def test_list_shows_short_commit_and_unpinned_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pinned entries show a short commit; experimental ref-only entries are marked unpinned."""
    configs_root = _configs_root(
        tmp_path,
        {
            "acme.yaml": _entry(),
            "beta.yaml": _entry(name="beta", status="experimental", commit=None, ref="main"),
        },
    )

    exit_code, output = _list(monkeypatch, configs_root)

    assert exit_code == 0, output
    assert "acme" in output
    assert COMMIT[:12] in output
    assert COMMIT not in output
    assert "main (unpinned)" in output


def test_list_hides_deprecated_unless_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Deprecated entries are hidden by default and shown with --all."""
    configs_root = _configs_root(
        tmp_path,
        {"acme.yaml": _entry(), "zeta.yaml": _entry(name="zeta", status="deprecated")},
    )

    _, default_output = _list(monkeypatch, configs_root)
    _, all_output = _list(monkeypatch, configs_root, "--all")

    assert "zeta" not in default_output
    assert "zeta" in all_output
    assert "deprecated" in all_output


def test_list_empty_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty registry is not an error."""
    exit_code, output = _list(monkeypatch, _configs_root(tmp_path, {}))

    assert exit_code == 0, output
    assert "No providers registered." in output


def test_list_skips_invalid_entries_with_a_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One unfinished entry, such as a scaffold's stub, is skipped with its problems; the others still list."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry(name="other"), "zeta.yaml": _entry(name="zeta")})

    exit_code, output = _list(monkeypatch, configs_root)

    assert exit_code == 0, output
    assert "Skipping invalid registry entry acme.yaml" in output
    assert "must match the filename 'acme'" in output
    assert "zeta" in output


def test_strict_loader_still_rejects_invalid_entries(tmp_path: Path) -> None:
    """The pre-commit hook and tests use the strict loader, so an unfinished entry cannot be committed."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry(name="other"), "zeta.yaml": _entry(name="zeta")})

    entries, problems = load_registry_skipping_invalid(configs_root)

    assert [entry.name for entry in entries] == ["zeta"]
    assert list(problems) == ["acme.yaml"]
    assert "must match the filename 'acme'" in _problems(configs_root)


def test_ensure_fetched_skips_invalid_entry_for_local_scaffold(tmp_path: Path) -> None:
    """An unfinished entry for a scaffold under development does not stop it from running."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry(commit="<validated commit>")})
    _local_provider(tmp_path, "acme")

    assert ensure_fetched("acme", configs_root) is None


def test_rejects_name_of_in_tree_provider(tmp_path: Path) -> None:
    """A registry name must not shadow an in-tree provider, or --provider would be ambiguous."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry()})
    (configs_root / "providers" / "acme").mkdir(parents=True)

    assert "acme.yaml: name: 'acme' is already an in-tree provider in providers/" in _problems(configs_root)


def _upstream_repo(tmp_path: Path) -> tuple[str, str, str]:
    """Create a two-commit provider repo with tag ``v1`` on the tip; return its file:// URL and both SHAs."""
    repo = tmp_path / "upstream"
    repo.mkdir()

    def git(*args: str) -> str:
        """Run git in the upstream repo without depending on the user's identity or signing config."""
        identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
        result = subprocess.run(["git", *identity, *args], cwd=repo, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    git("init", "-q")
    (repo / "config").mkdir()
    (repo / "config" / "vm.yaml").write_text("tests: {}\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    first = git("rev-parse", "HEAD")
    (repo / "config" / "network.yaml").write_text("tests: {}\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "second")
    git("tag", "v1")
    return repo.as_uri(), first, git("rev-parse", "HEAD")


def _registry_entry(repo_url: str, **overrides: Any) -> RegistryEntry:
    """Return a qualified entry for ``repo_url``; the https rule is the loader's, so file:// is allowed here."""
    entry = RegistryEntry(
        name="acme",
        vendor="Acme Cloud Inc.",
        description="Acme GPU instances.",
        repo_url=repo_url,
        commit=None,
        ref=None,
        tested_with="0.13.0",
        suites=("vm",),
        maintainers=(Maintainer(github="acme-handle", email=None),),
        documentation_url="https://example.com/acme",
        status="qualified",
    )
    return dataclasses.replace(entry, **overrides)


def _fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: RegistryEntry, *args: str) -> tuple[int, str]:
    """Run ``isvctl provider fetch`` against ``tmp_path/configs`` with the registry replaced by one entry."""
    monkeypatch.setattr(provider_cli, "CONFIGS_ROOT", tmp_path / "configs")
    monkeypatch.setattr(provider_cli, "load_registry_skipping_invalid", lambda configs_root: ([entry], {}))
    result = runner.invoke(provider_cli.app, ["fetch", *args])
    return result.exit_code, result.output


def _checkout(tmp_path: Path, name: str = "acme") -> Path:
    """Return where ``provider fetch`` checks ``name`` out under ``tmp_path/configs``."""
    return tmp_path / "configs" / "providers-external" / name


def _head(checkout: Path) -> str:
    """Return the commit checked out in ``checkout``."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_fetch_checks_out_pinned_commit_and_suggests_run_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pinned (older) commit lands in providers-external/<name>, and the run-by-name command is printed."""
    url, first, _ = _upstream_repo(tmp_path)

    exit_code, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first), "acme")

    checkout = _checkout(tmp_path)
    assert exit_code == 0, output
    assert _head(checkout) == first
    assert (checkout / "config" / "vm.yaml").is_file()
    assert not (checkout / "config" / "network.yaml").exists()
    assert f"Fetched acme at {first[:12]} into" in output
    assert first not in output
    assert "Suites: vm" in output
    assert "Setup and prerequisites (credentials, environment): https://example.com/acme" in output
    assert "uv run isvctl test run --provider acme --suite vm" in output


def test_refetch_replaces_previous_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fetching again replaces the checkout, including local edits, and leaves no staging directories."""
    url, first, tip = _upstream_repo(tmp_path)
    _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first), "acme")
    (_checkout(tmp_path) / "local-edit.txt").write_text("edit\n", encoding="utf-8")

    exit_code, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=tip), "acme")

    assert exit_code == 0, output
    assert _head(_checkout(tmp_path)) == tip
    assert not (_checkout(tmp_path) / "local-edit.txt").exists()
    assert [path.name for path in _checkout(tmp_path).parent.iterdir()] == ["acme"]


def test_fetch_experimental_ref_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ref-only entry fetches the ref's current commit and warns that results are not reproducible."""
    url, _, tip = _upstream_repo(tmp_path)

    exit_code, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, status="experimental", ref="v1"), "acme")

    assert exit_code == 0, output
    assert "not reproducible" in output
    assert _head(_checkout(tmp_path)) == tip


def test_failed_fetch_keeps_previous_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A commit the repo does not have fails the command and leaves the existing checkout untouched."""
    url, first, _ = _upstream_repo(tmp_path)
    _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first), "acme")

    exit_code, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, commit="f" * 40), "acme")

    assert exit_code == 1
    assert "Could not fetch 'acme'" in output
    assert _head(_checkout(tmp_path)) == first
    assert [path.name for path in _checkout(tmp_path).parent.iterdir()] == ["acme"]


def test_fetch_treats_option_like_ref_as_a_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A registry ref that looks like a git option is passed after ``--`` and never executed."""
    url, _, _ = _upstream_repo(tmp_path)
    marker = tmp_path / "marker"
    entry = _registry_entry(url, status="experimental", ref=f"--upload-pack=touch {marker}")

    exit_code, _ = _fetch(monkeypatch, tmp_path, entry, "acme")

    assert exit_code == 1
    assert not marker.exists()


def test_fetch_ignores_inherited_git_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A GIT_DIR exported by a calling git hook must not redirect the fetch into the caller's repo."""
    url, first, _ = _upstream_repo(tmp_path)
    caller_git_dir = tmp_path / "caller.git"
    monkeypatch.setenv("GIT_DIR", str(caller_git_dir))

    exit_code, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first), "acme")

    monkeypatch.delenv("GIT_DIR")
    assert exit_code == 0, output
    assert _head(_checkout(tmp_path)) == first
    assert not caller_git_dir.exists()


def test_fetch_unknown_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unregistered name fails and points at the list command."""
    entry = _registry_entry("https://example.com/acme", commit=COMMIT)

    exit_code, output = _fetch(monkeypatch, tmp_path, entry, "nope")

    assert exit_code == 1
    assert "Unknown provider 'nope'" in output
    assert "isvctl provider list --all" in output


def test_fetch_reports_missing_suite_configs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When no declared suite has a config/<suite>.yaml, the checkout path is printed instead of a command."""
    url, first, _ = _upstream_repo(tmp_path)

    exit_code, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first, suites=("storage",)), "acme")

    assert exit_code == 0, output
    assert "No config/<suite>.yaml found for suites storage" in output
    assert "uv run isvctl test run" not in output


def _remove(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> tuple[int, str]:
    """Run ``isvctl provider remove`` against ``tmp_path/configs``."""
    monkeypatch.setattr(provider_cli, "CONFIGS_ROOT", tmp_path / "configs")
    result = runner.invoke(provider_cli.app, ["remove", *args])
    return result.exit_code, result.output


def _fake_checkouts(tmp_path: Path, *names: str) -> Path:
    """Create checkouts under ``tmp_path/configs/providers-external`` carrying the fetch marker."""
    for name in names:
        (_checkout(tmp_path, name) / "config").mkdir(parents=True)
        (_checkout(tmp_path, name) / ".git").mkdir()
        mark_fetched(_checkout(tmp_path, name))
    return _checkout(tmp_path, names[0]).parent


def _local_provider(tmp_path: Path, name: str) -> Path:
    """Create a provider under providers-external/ that fetch did not make, like a scaffold in development."""
    local = _checkout(tmp_path, name)
    (local / ".git").mkdir(parents=True)
    (local / "scripts").mkdir()
    return local


def test_remove_all_keeps_local_providers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--all deletes only fetched checkouts, never a provider someone is developing there."""
    external_dir = _fake_checkouts(tmp_path, "acme")
    _local_provider(tmp_path, "mine")

    exit_code, output = _remove(monkeypatch, tmp_path, "--all")

    assert exit_code == 0, output
    assert "Removed 1 fetched provider(s): acme" in output
    assert "Kept (not created by fetch; add --force to delete them too): mine" in output
    assert [path.name for path in external_dir.iterdir()] == ["mine"]


def test_remove_refuses_local_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Removing a provider fetch did not create is refused without --force, and deletes nothing."""
    local = _local_provider(tmp_path, "mine")

    exit_code, output = _remove(monkeypatch, tmp_path, "mine")

    assert exit_code == 1
    assert "Not created by fetch (your own work?): mine. Add --force to delete it anyway." in output
    assert local.is_dir()


def test_remove_force_deletes_local_provider_but_not_its_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--force deletes a local provider; its registry entry is left in place and pointed out."""
    configs_root = _configs_root(tmp_path, {"mine.yaml": _entry(name="mine")})
    local = _local_provider(tmp_path, "mine")

    exit_code, output = _remove(monkeypatch, tmp_path, "mine", "--force")

    assert exit_code == 0, output
    assert not local.exists()
    assert (configs_root / "providers-registry" / "mine.yaml").is_file()
    assert "Left its registry entry in place:" in output


def test_remove_all_force_deletes_everything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--all --force deletes fetched and local providers alike."""
    external_dir = _fake_checkouts(tmp_path, "acme")
    _local_provider(tmp_path, "mine")

    exit_code, output = _remove(monkeypatch, tmp_path, "--all", "--force")

    assert exit_code == 0, output
    assert "Removed 2 provider(s): acme, mine" in output
    assert list(external_dir.iterdir()) == []


def test_fetch_refuses_to_replace_local_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fetching a name whose directory fetch did not create leaves that work untouched."""
    url, first, _ = _upstream_repo(tmp_path)
    local = _local_provider(tmp_path, "acme")

    exit_code, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first), "acme")

    assert exit_code == 1
    assert "was not created by `isvctl provider fetch`" in output
    assert (local / "scripts").is_dir()


def test_local_provider_runs_without_fetch_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A registered name with a local (non-fetched) directory is the partner's own work: run it, list it as local."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry()})
    _local_provider(tmp_path, "acme")

    entry = ensure_fetched("acme", configs_root)
    _, output = _list(monkeypatch, configs_root)

    assert entry is not None and entry.name == "acme"
    assert "local" in output


def test_remove_one_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Removing a provider deletes only its checkout."""
    external_dir = _fake_checkouts(tmp_path, "acme", "beta")

    exit_code, output = _remove(monkeypatch, tmp_path, "acme")

    assert exit_code == 0, output
    assert "Removed acme" in output
    assert [path.name for path in external_dir.iterdir()] == ["beta"]


def test_remove_all_providers_and_staging_leftovers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--all removes every fetched provider, including staging directories from an interrupted fetch."""
    external_dir = _fake_checkouts(tmp_path, "acme", "beta")
    (external_dir / ".fetch-acme-x1y2").mkdir()

    exit_code, output = _remove(monkeypatch, tmp_path, "--all")

    assert exit_code == 0, output
    assert "Removed 2 fetched provider(s): acme, beta" in output
    assert list(external_dir.iterdir()) == []


def test_remove_all_with_nothing_fetched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--all on an empty or missing providers-external/ is not an error."""
    exit_code, output = _remove(monkeypatch, tmp_path, "--all")

    assert exit_code == 0, output
    assert "Removed 0 fetched provider(s)." in output


@pytest.mark.parametrize("force", [(), ("--force",)])
def test_remove_refuses_missing_or_escaping_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, force: tuple[str, ...]
) -> None:
    """Names that are absent, or that would leave providers-external/, remove nothing, even with --force."""
    external_dir = _fake_checkouts(tmp_path, "acme")
    in_tree = tmp_path / "configs" / "providers" / "aws"
    in_tree.mkdir(parents=True)

    exit_code, output = _remove(monkeypatch, tmp_path, "acme", "nope", "../providers/aws", *force)

    assert exit_code == 1
    assert "Not in providers-external/: nope, ../providers/aws" in output
    assert (external_dir / "acme").is_dir()
    assert in_tree.is_dir()


@pytest.mark.parametrize("args", [(), ("acme", "--all")])
def test_remove_needs_names_or_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: tuple[str, ...]) -> None:
    """Exactly one of provider names or --all must be given."""
    exit_code, output = _remove(monkeypatch, tmp_path, *args)

    assert exit_code == 1
    assert "Give one or more provider names, or --all." in output


def test_list_shows_fetch_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Fetched column distinguishes not fetched, fetched at the pin, and fetched at another commit."""
    url, first, tip = _upstream_repo(tmp_path)
    configs_root = _configs_root(
        tmp_path,
        {
            "acme.yaml": _entry(commit=first),
            "beta.yaml": _entry(name="beta", commit=first),
            "zeta.yaml": _entry(name="zeta", commit=first),
        },
    )
    for name, commit in (("acme", first), ("beta", tip)):
        _fetch(monkeypatch, tmp_path, _registry_entry(url, name=name, commit=commit), name)
    monkeypatch.setattr(provider_cli, "load_registry_skipping_invalid", load_registry_skipping_invalid)

    exit_code, output = _list(monkeypatch, configs_root)

    assert exit_code == 0, output

    rows = {line.split()[1]: line for line in output.splitlines() if line.startswith("│")}
    assert rows["acme"].rstrip("│ ").endswith("yes")
    assert f"stale ({tip[:12]})" in rows["beta"]
    assert rows["zeta"].rstrip("│ ").endswith("no")


def test_ensure_fetched_accepts_checkout_at_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A registered provider fetched at its pinned commit is ready to run."""
    url, first, _ = _upstream_repo(tmp_path)
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry(commit=first)})
    _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first), "acme")

    ensure_fetched("acme", configs_root)


def test_ensure_fetched_rejects_stale_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A checkout at another commit than the registry pins, e.g. after a pull, must be fetched again."""
    url, first, tip = _upstream_repo(tmp_path)
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry(commit=tip)})
    _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first), "acme")

    with pytest.raises(ProviderRegistryError, match=f"fetched at {first[:12]}, but the registry pins {tip[:12]}"):
        ensure_fetched("acme", configs_root)


def test_ensure_fetched_rejects_registered_but_not_fetched(tmp_path: Path) -> None:
    """A registered provider that was never fetched points at the fetch command."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry()})

    with pytest.raises(
        ProviderRegistryError, match=re.escape("registered but not fetched. Run: isvctl provider fetch acme")
    ):
        ensure_fetched("acme", configs_root)


def test_test_run_by_name_requires_a_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`test run --provider <registered name>` stops with the fetch command before running anything."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry()})
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--suite", "vm", "--no-upload"])

    assert result.exit_code == 1
    assert "registered but not fetched. Run: isvctl provider fetch acme" in result.output


def test_demo_status_requires_a_pinned_commit(tmp_path: Path) -> None:
    """`demo` is a status like the others: only `experimental` may omit the commit."""
    [demo] = load_registry(_configs_root(tmp_path / "pinned", {"acme.yaml": _entry(status="demo")}))
    problems = _problems(_configs_root(tmp_path / "unpinned", {"acme.yaml": _entry(status="demo", commit=None)}))

    assert demo.status == "demo"
    assert "'commit' is a required property" in problems


def test_fetch_says_demo_provider_needs_no_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """fetch tells a demo provider needs no setup instead of linking prerequisites."""
    url, first, _ = _upstream_repo(tmp_path)

    _, output = _fetch(monkeypatch, tmp_path, _registry_entry(url, commit=first, status="demo"), "acme")

    assert "Status 'demo': it runs in demo mode (dummy results, never uploaded) with no setup." in output
    assert "Setup and prerequisites" not in output


class _EnvCapturingOrchestrator:
    """Record the demo-mode variable the steps would inherit, instead of running anything."""

    demo_mode: ClassVar[str | None] = None

    def __init__(self, config: Any, **kwargs: Any) -> None:
        """Accept the CLI's orchestrator arguments."""

    def run(self, **kwargs: Any) -> OrchestratorResult:
        """Capture ISVCTL_DEMO_MODE as a step subprocess would see it."""
        type(self).demo_mode = os.environ.get("ISVCTL_DEMO_MODE")
        return OrchestratorResult(success=True, phases=[PhaseResult(phase=Phase.TEST, success=True, message="ok")])


def _no_upload_allowed() -> tuple[bool, str, str]:
    """Stand in for the credential check, which a demo run must never reach."""
    raise AssertionError("a demo-mode run must not attempt an upload")


def test_test_run_turns_on_demo_mode_for_demo_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`test run --provider <demo provider>` sets ISVCTL_DEMO_MODE=1 for its steps and never uploads."""
    configs_root = _configs_root(tmp_path, {})
    (configs_root / "suites" / "vm.yaml").write_text("tests:\n  capability: vm\n  validations: {}\n", encoding="utf-8")
    checkout = configs_root / "providers-external" / "acme"
    (checkout / "config").mkdir(parents=True)
    (checkout / "config" / "vm.yaml").write_text(
        "import: ../../../suites/vm.yaml\ncommands:\n  vm:\n    phases: [test]\n    steps: []\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fetched",
        ],
        cwd=checkout,
        check=True,
    )
    (configs_root / "providers-registry" / "acme.yaml").write_text(
        yaml.safe_dump(_entry(status="demo", commit=_head(checkout))), encoding="utf-8"
    )
    # Registers the variable for restoration; the CLI overwrites it in os.environ.
    monkeypatch.setenv("ISVCTL_DEMO_MODE", "0")
    monkeypatch.setattr(test_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(test_cli, "Orchestrator", _EnvCapturingOrchestrator)
    monkeypatch.setattr(test_cli, "check_upload_credentials", _no_upload_allowed)
    _EnvCapturingOrchestrator.demo_mode = None

    result = runner.invoke(test_cli.app, ["run", "--provider", "acme", "--suite", "vm"])

    assert result.exit_code == 0, result.output
    assert "'acme' has status 'demo': running with ISVCTL_DEMO_MODE=1." in result.output
    assert "never uploaded to ISV Lab Service" in result.output
    assert _EnvCapturingOrchestrator.demo_mode == "1"


def test_demo_mode_never_uploads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With ISVCTL_DEMO_MODE=1 set by hand, results are not uploaded even without --no-upload."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "commands:\n  vm:\n    phases: [test]\n    steps: []\ntests:\n  capability: vm\n  validations: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ISVCTL_DEMO_MODE", "1")
    monkeypatch.setattr(test_cli, "Orchestrator", _EnvCapturingOrchestrator)
    monkeypatch.setattr(test_cli, "check_upload_credentials", _no_upload_allowed)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config)])

    assert result.exit_code == 0, result.output
    assert "never uploaded to ISV Lab Service" in result.output


def test_ensure_fetched_ignores_in_tree_and_unknown_providers(tmp_path: Path) -> None:
    """In-tree providers and unregistered names are left to the caller's own handling."""
    configs_root = _configs_root(tmp_path, {"acme.yaml": _entry()})
    (configs_root / "providers" / "aws").mkdir(parents=True)

    ensure_fetched("aws", configs_root)
    ensure_fetched("nope", configs_root)
