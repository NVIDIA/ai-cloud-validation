# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the external provider registry loader and its CLI commands."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from isvctl.cli import provider as provider_cli
from isvctl.config.provider_registry import Maintainer, ProviderRegistryError, RegistryEntry, load_registry

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
    registry_dir = configs_root / "provider-registry"
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
    (configs_root / "provider-registry" / "acme.yaml").write_text("name: [unclosed\n", encoding="utf-8")

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


def test_list_fails_on_invalid_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An invalid entry fails the command and names the problem."""
    exit_code, output = _list(monkeypatch, _configs_root(tmp_path, {"acme.yaml": _entry(name="other")}))

    assert exit_code == 1
    assert "must match the filename 'acme'" in output


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


def _fetch(monkeypatch: pytest.MonkeyPatch, entry: RegistryEntry, *args: str) -> tuple[int, str]:
    """Run ``isvctl provider fetch`` with the registry replaced by one entry."""
    monkeypatch.setattr(provider_cli, "load_registry", lambda configs_root: [entry])
    result = runner.invoke(provider_cli.app, ["fetch", *args])
    return result.exit_code, result.output


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


def _head(checkout: Path) -> str:
    """Return the commit checked out in ``checkout``."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_fetch_checks_out_pinned_commit_not_tip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The pinned (older) commit is checked out, and the matching suite config is suggested."""
    url, first, _ = _upstream_repo(tmp_path)
    dest = tmp_path / "checkout"

    exit_code, output = _fetch(monkeypatch, _registry_entry(url, commit=first), "acme", "--dest", str(dest))

    assert exit_code == 0, output
    assert _head(dest) == first
    assert (dest / "config" / "vm.yaml").is_file()
    assert not (dest / "config" / "network.yaml").exists()
    assert "uv run isvctl test run -f" in output
    assert "config/vm.yaml" in output


def test_fetch_defaults_to_cache_and_refetch_replaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without --dest the checkout lands in the XDG cache keyed by commit, and fetching again replaces it."""
    url, first, _ = _upstream_repo(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    entry = _registry_entry(url, commit=first)
    provider_dir = tmp_path / "cache" / "isvctl" / "providers" / "acme"

    first_exit, first_output = _fetch(monkeypatch, entry, "acme")
    (provider_dir / first / "stale.txt").write_text("local edit\n", encoding="utf-8")
    second_exit, second_output = _fetch(monkeypatch, entry, "acme")

    assert first_exit == 0, first_output
    assert second_exit == 0, second_output
    assert [path.name for path in provider_dir.iterdir()] == [first]
    assert _head(provider_dir / first) == first
    assert not (provider_dir / first / "stale.txt").exists()


def test_fetch_experimental_ref_warns_and_keys_by_resolved_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ref-only entry fetches the ref's current commit, stores it under that SHA, and warns."""
    url, _, tip = _upstream_repo(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    entry = _registry_entry(url, status="experimental", ref="v1")

    exit_code, output = _fetch(monkeypatch, entry, "acme")

    assert exit_code == 0, output
    assert "not reproducible" in output
    assert _head(tmp_path / "cache" / "isvctl" / "providers" / "acme" / tip) == tip


def test_fetch_missing_commit_fails_without_leftovers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A commit the repo does not have fails the command and leaves no partial checkout."""
    url, _, _ = _upstream_repo(tmp_path)
    dest = tmp_path / "out" / "checkout"

    exit_code, output = _fetch(monkeypatch, _registry_entry(url, commit="f" * 40), "acme", "--dest", str(dest))

    assert exit_code == 1
    assert "Could not fetch 'acme'" in output
    assert list((tmp_path / "out").iterdir()) == []


def test_fetch_treats_option_like_ref_as_a_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A registry ref that looks like a git option is passed after ``--`` and never executed."""
    url, _, _ = _upstream_repo(tmp_path)
    marker = tmp_path / "marker"
    entry = _registry_entry(url, status="experimental", ref=f"--upload-pack=touch {marker}")

    exit_code, _ = _fetch(monkeypatch, entry, "acme", "--dest", str(tmp_path / "checkout"))

    assert exit_code == 1
    assert not marker.exists()


def test_fetch_ignores_inherited_git_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A GIT_DIR exported by a calling git hook must not redirect the fetch into the caller's repo."""
    url, first, _ = _upstream_repo(tmp_path)
    caller_git_dir = tmp_path / "caller.git"
    monkeypatch.setenv("GIT_DIR", str(caller_git_dir))
    dest = tmp_path / "checkout"

    exit_code, output = _fetch(monkeypatch, _registry_entry(url, commit=first), "acme", "--dest", str(dest))

    monkeypatch.delenv("GIT_DIR")
    assert exit_code == 0, output
    assert _head(dest) == first
    assert not caller_git_dir.exists()


def test_fetch_refuses_existing_dest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An existing --dest is never overwritten."""
    url, first, _ = _upstream_repo(tmp_path)
    dest = tmp_path / "checkout"
    dest.mkdir()

    exit_code, output = _fetch(monkeypatch, _registry_entry(url, commit=first), "acme", "--dest", str(dest))

    assert exit_code == 1
    assert "Destination already exists" in output
    assert list(dest.iterdir()) == []


def test_fetch_unknown_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unregistered name fails and points at the list command."""
    exit_code, output = _fetch(monkeypatch, _registry_entry("https://example.com/acme", commit=COMMIT), "nope")

    assert exit_code == 1
    assert "Unknown provider 'nope'" in output
    assert "isvctl provider list --all" in output


def test_fetch_reports_missing_suite_configs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When no declared suite has a config/<suite>.yaml, the checkout path is printed instead of a command."""
    url, first, _ = _upstream_repo(tmp_path)
    entry = _registry_entry(url, commit=first, suites=("storage",))

    exit_code, output = _fetch(monkeypatch, entry, "acme", "--dest", str(tmp_path / "checkout"))

    assert exit_code == 0, output
    assert "No config/<suite>.yaml found for suites storage" in output
    assert "uv run isvctl test run" not in output
