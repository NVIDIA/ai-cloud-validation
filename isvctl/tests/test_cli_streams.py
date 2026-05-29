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
        "module": "isvtest.validations.alpha",
        "platforms": ["KUBERNETES"],
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
  platform: kubernetes
  validations: {}
""",
        encoding="utf-8",
    )
    return config


def test_dry_run_stdout_is_pure_json(tmp_path: Path) -> None:
    """`test run --dry-run` emits only JSON on stdout; progress goes to stderr."""
    config = _write_config(tmp_path)

    result = runner.invoke(test_cli.app, ["run", "-f", str(config), "--no-upload", "--dry-run"])

    assert result.exit_code == 0, result.output
    # stdout must be parseable JSON with nothing else mixed in.
    payload = json.loads(result.stdout)
    assert payload["tests"]["platform"] == "kubernetes"
    # Progress lives on stderr, never on the machine-readable stdout stream.
    assert "Validating configuration" in result.stderr
    assert "Validating configuration" not in result.stdout
    assert "--- Dry Run: Configuration ---" not in result.stdout


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
