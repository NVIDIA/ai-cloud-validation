# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the external provider registry loader and its CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from isvctl.cli import provider as provider_cli
from isvctl.config.provider_registry import Maintainer, ProviderRegistryError, load_registry

COMMIT = "0123456789abcdef0123456789abcdef01234567"

runner = CliRunner()


def _entry(**overrides: Any) -> dict[str, Any]:
    """Return a valid qualified entry named ``acme``, with fields overridden or removed (``None``)."""
    entry: dict[str, Any] = {
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
