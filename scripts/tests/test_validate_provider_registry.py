# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for validate_provider_registry.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "validate_provider_registry", Path(__file__).resolve().parent.parent / "validate_provider_registry.py"
)
assert _spec and _spec.loader
validate_provider_registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_provider_registry)


def _configs_root(tmp_path: Path, entry_name: str) -> Path:
    """Write a registry with one entry whose ``name`` is ``entry_name``, in file ``acme.yaml``."""
    configs_root = tmp_path / "configs"
    (configs_root / "suites").mkdir(parents=True)
    (configs_root / "suites" / "vm.yaml").write_text("tests: {}\n", encoding="utf-8")
    (configs_root / "provider-registry").mkdir()
    (configs_root / "provider-registry" / "acme.yaml").write_text(
        f"""\
schema_version: 1
name: {entry_name}
vendor: Acme Cloud Inc.
description: Acme GPU instances.
repo_url: https://github.com/acme/isvctl-provider-acme
commit: 0123456789abcdef0123456789abcdef01234567
tested_with: "0.13.0"
suites: [vm]
maintainers: [{{github: acme-handle}}]
documentation_url: https://github.com/acme/isvctl-provider-acme#reproducing
status: qualified
""",
        encoding="utf-8",
    )
    return configs_root


def test_committed_registry_is_valid() -> None:
    """The registry in this checkout passes the same check the pre-commit hook runs."""
    assert validate_provider_registry.main(["--check"]) == 0


def test_valid_entry_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A valid registry exits 0 and reports how many entries it checked."""
    assert validate_provider_registry.main(["--check"], _configs_root(tmp_path, "acme")) == 0
    assert "OK: 1 provider registry entries are valid." in capsys.readouterr().out


def test_check_fails_on_invalid_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--check exits 1 and writes the loader's problems to stderr."""
    assert validate_provider_registry.main(["--check"], _configs_root(tmp_path, "other")) == 1
    assert "must match the filename 'acme'" in capsys.readouterr().err


def test_report_mode_prints_problems_and_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Without --check, problems are printed but do not fail the run."""
    assert validate_provider_registry.main([], _configs_root(tmp_path, "other")) == 0
    assert "must match the filename 'acme'" in capsys.readouterr().out
