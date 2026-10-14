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

"""Stream-discipline regression tests for the isvctl CLI.

Diagnostics (progress, warnings, errors) must go to stderr; stdout is reserved
for primary machine-consumable output. These tests pin that contract using the
CliRunner's separate ``result.stdout`` / ``result.stderr`` streams (Typer keeps
the mixed view in ``result.output``, which the older CLI tests rely on).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import isvctl.cli.catalog as catalog_cli
import isvctl.cli.clean as clean_cli
import isvctl.cli.test as test_cli

runner = CliRunner()

_FAKE_ENTRIES = [
    {
        "name": "AlphaCheck",
        "description": "Alpha description",
        "labels": ["kubernetes"],
        "source": "isvtest.validations.alpha",
        "suite": "kubernetes",
        "platform": "kubernetes",
        "requires": [],
    },
]


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal, self-contained isvctl test config (no imports)."""
    config = tmp_path / "config.yaml"
    config.write_text(
        """
commands:
  kubernetes:
    phases: [test]
    steps:
      - name: test_step
        command: echo
        args: ['{"success": true}']
        phase: test
tests:
  capability: kubernetes
  validations: {}
""",
        encoding="utf-8",
    )
    return config


def test_dry_run_stdout_is_human_readable(tmp_path: Path) -> None:
    """`test run --dry-run` summarizes the execution plan for operators."""
    config = _write_config(tmp_path)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--no-upload", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry-run plan" in result.stdout
    assert "Suite type: platform (kubernetes)" in result.stdout
    assert "Checks: 0" in result.stdout
    assert "Validating configuration" in result.stderr
    assert "Validating configuration" not in result.stdout


def test_dry_run_applies_the_config_exclude_block(tmp_path: Path) -> None:
    """A plan that ignored the config's own excludes would promise skipped checks."""
    config = tmp_path / "excludes.yaml"
    config.write_text(
        """
commands:
  kubernetes:
    phases: [test]
    steps:
      - name: test_step
        command: echo
        args: ['{"success": true}']
        phase: test
tests:
  capability: kubernetes
  validations:
    checks_group:
      step: test_step
      checks:
        K8sNodeCountCheck:
          test_id: "N/A"
          labels: ["kubernetes"]
        K8sCncfConformanceCheck:
          test_id: "N/A"
          labels: ["kubernetes", "slow"]
  exclude:
    labels: [slow]
    tests: [K8sNodeCountCheck]
""",
        encoding="utf-8",
    )

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--no-upload", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Excluded labels: slow" in result.stdout
    assert "Excluded tests: K8sNodeCountCheck" in result.stdout
    assert "[SKIP] K8sNodeCountCheck: excluded by name" in result.stdout
    assert "[SKIP] K8sCncfConformanceCheck: excluded by label: slow" in result.stdout

    for selection in (["--label", "kubernetes"], ["--", "-m", "kubernetes"]):
        selected = runner.invoke(
            test_cli.app,
            ["run", "-f", str(config), "--no-upload", "--dry-run", *selection],
        )

        assert selected.exit_code == 0, selected.output
        assert "Excluded labels: slow" not in selected.stdout
        assert "[SKIP] K8sNodeCountCheck: excluded by name" in selected.stdout
        assert "[RUN]  K8sCncfConformanceCheck" in selected.stdout


def test_catalog_list_json_stdout_is_pure_json() -> None:
    """`catalog list --json` keeps stdout free of diagnostics."""
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=_FAKE_ENTRIES),
        patch("isvctl.cli.catalog.get_catalog_version", return_value="1.2.3"),
    ):
        result = runner.invoke(catalog_cli.app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["isvTestVersion"] == "1.2.3"
    assert payload["entries"] == _FAKE_ENTRIES


def test_clean_run_unknown_operation_error_on_stderr() -> None:
    """A bad `clean run` operation reports the error on stderr, leaving stdout clean."""
    result = runner.invoke(clean_cli.app, ["run", "bogus-op"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "Unknown operation" in result.stderr
    # Nothing should be written to stdout for a pure error path.
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    ("pytest_args", "exit_code", "diagnostic"),
    [
        (["not_a_real_path"], 1, "pytest failed: usage error (exit code 4)"),
        (["-k", "("], 1, "pytest failed: usage error (exit code 4)"),
        (["-k", "not K8sNodeCountCheck"], 0, "WARNING: No tests collected by pytest"),
        (["-m", "nonexistent_marker"], 0, "WARNING: No tests collected by pytest"),
        ([], 0, "All phases completed successfully"),
    ],
)
def test_run_reports_pytest_session_status(
    tmp_path: Path, pytest_args: list[str], exit_code: int, diagnostic: str
) -> None:
    """Real pytest usage errors fail the CLI; empty selections warn and still succeed."""
    config = tmp_path / "validations.yaml"
    config.write_text(
        "tests:\n  validations:\n    smoke:\n      checks:\n        K8sNodeCountCheck: {}\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        test_cli.app,
        [
            "run",
            "-f",
            str(config),
            "--phase",
            "test",
            "--no-upload",
            "--junitxml",
            str(tmp_path / "junit.xml"),
            "--",
            *pytest_args,
        ],
    )

    assert result.exit_code == exit_code, result.output
    assert diagnostic in result.stdout
    if exit_code:
        assert "[FAIL] TEST" in result.stdout
        assert "[FAIL] Orchestration failed" in result.stdout
        assert "All phases completed successfully" not in result.stdout
