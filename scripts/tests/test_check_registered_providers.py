# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for check_registered_providers.py."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_registered_providers", Path(__file__).resolve().parent.parent / "check_registered_providers.py"
)
assert _spec and _spec.loader
check_registered_providers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_registered_providers)


def _configs_root(tmp_path: Path, statuses: dict[str, str]) -> Path:
    """Write a registry with one entry per ``name: status``, each declaring suites vm and iam."""
    configs_root = tmp_path / "configs"
    (configs_root / "suites").mkdir(parents=True)
    for suite in ("vm", "iam"):
        (configs_root / "suites" / f"{suite}.yaml").write_text("tests: {}\n", encoding="utf-8")
    (configs_root / "providers-registry").mkdir()
    for name, status in statuses.items():
        (configs_root / "providers-registry" / f"{name}.yaml").write_text(
            f"""\
schema_version: 1
name: {name}
vendor: Acme Cloud Inc.
description: Acme GPU instances.
repo_url: https://github.com/acme/isvctl-provider-{name}
commit: 0123456789abcdef0123456789abcdef01234567
tested_with: "0.13.0"
suites: [vm, iam]
maintainers: [{{github: acme-handle}}]
documentation_url: https://github.com/acme/isvctl-provider-{name}#reproducing
status: {status}
""",
            encoding="utf-8",
        )
    return configs_root


@pytest.fixture
def isvctl_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record isvctl invocations instead of running them; a call containing 'fail' exits 1."""
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        args = command[len(check_registered_providers.ISVCTL) :]
        calls.append(args)
        return subprocess.CompletedProcess(command, 1 if "fail" in args else 0)

    monkeypatch.setattr(check_registered_providers.subprocess, "run", fake_run)
    return calls


def test_checks_depend_on_status(tmp_path: Path, isvctl_calls: list[list[str]]) -> None:
    """Supported entries are fetched and dry-run, demo entries also run, deprecated ones are skipped."""
    configs_root = _configs_root(tmp_path, {"acme": "supported", "demo1": "demo", "old": "deprecated"})

    assert check_registered_providers.main([], configs_root) == 0
    assert isvctl_calls == [
        ["provider", "fetch", "acme"],
        ["test", "run", "--provider", "acme", "--suite", "vm", "--dry-run"],
        ["test", "run", "--provider", "acme", "--suite", "iam", "--dry-run"],
        ["provider", "fetch", "demo1"],
        ["test", "run", "--provider", "demo1", "--suite", "vm", "--dry-run"],
        ["test", "run", "--provider", "demo1", "--suite", "vm"],
        ["test", "run", "--provider", "demo1", "--suite", "iam", "--dry-run"],
        ["test", "run", "--provider", "demo1", "--suite", "iam"],
    ]


def test_failure_stops_that_entry_and_exits_1(
    tmp_path: Path, isvctl_calls: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing command stops checking that entry, the others still run, and the exit code is 1."""
    configs_root = _configs_root(tmp_path, {"acme": "supported", "fail": "supported"})

    assert check_registered_providers.main([], configs_root) == 1
    assert ["provider", "fetch", "fail"] in isvctl_calls
    assert ["test", "run", "--provider", "fail", "--suite", "vm", "--dry-run"] not in isvctl_calls
    assert ["test", "run", "--provider", "acme", "--suite", "iam", "--dry-run"] in isvctl_calls
    assert "FAILED: fail" in capsys.readouterr().err


def test_named_entries_only(tmp_path: Path, isvctl_calls: list[list[str]]) -> None:
    """Given names, only those entries are checked."""
    configs_root = _configs_root(tmp_path, {"acme": "supported", "other": "supported"})

    assert check_registered_providers.main(["other"], configs_root) == 0
    assert {call[2] for call in isvctl_calls if call[:2] == ["provider", "fetch"]} == {"other"}


def test_unknown_name_exits_1(tmp_path: Path, isvctl_calls: list[list[str]]) -> None:
    """A name that is not in the registry fails before anything is fetched."""
    configs_root = _configs_root(tmp_path, {"acme": "supported"})

    assert check_registered_providers.main(["nope"], configs_root) == 1
    assert isvctl_calls == []


def test_invalid_registry_exits_1(tmp_path: Path, isvctl_calls: list[list[str]]) -> None:
    """An invalid registry fails before anything is fetched."""
    configs_root = _configs_root(tmp_path, {"acme": "unknown"})

    assert check_registered_providers.main([], configs_root) == 1
    assert isvctl_calls == []
