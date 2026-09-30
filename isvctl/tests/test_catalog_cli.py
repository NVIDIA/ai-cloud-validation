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

"""Unit tests for the catalog CLI subcommand."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from isvtest.catalog import catalog_digest
from typer.testing import CliRunner

from isvctl.cli.catalog import app

runner = CliRunner()

_FAKE_ENTRIES = [
    {
        "name": "AlphaCheck",
        "description": "Alpha description",
        "labels": ["kubernetes"],
        "source": "isvtest.validations.alpha",
        "suite": "kubernetes",
        "capability": "kubernetes",
        "requires": [],
    },
    {
        "name": "BetaCheck",
        "description": "",
        "labels": [],
        "source": "isvtest.validations.beta",
        "suite": "storage",
        "capability": None,
        "requires": ["vm", "bare_metal"],
    },
    {
        "name": "GammaCheck",
        "description": "Core plain-suite check",
        "labels": ["iam"],
        "source": "isvtest.validations.gamma",
        "suite": "iam",
        "capability": None,
        "requires": [],
    },
]


def test_catalog_help() -> None:
    """Top-level catalog help mentions the new list command."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "list" in result.output


def test_catalog_list_table() -> None:
    """`catalog list` renders suite vs requires correctly for each entry kind."""
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=_FAKE_ENTRIES),
        patch("isvctl.cli.catalog.get_catalog_version", return_value="1.2.3"),
    ):
        result = runner.invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert "AlphaCheck" in result.output
    assert "BetaCheck" in result.output
    assert "GammaCheck" in result.output
    assert "1.2.3" in result.output
    # Platform suite: capability identity only (not "kubernetes / kubernetes").
    assert "kubernetes /" not in result.output
    # Plain suite with requires, and core (empty requires).
    assert "storage / vm, bare_metal" in result.output
    assert "iam / core" in result.output


def test_catalog_list_json() -> None:
    """`catalog list --json` emits parseable JSON matching the saved artifact shape."""
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=_FAKE_ENTRIES),
        patch("isvctl.cli.catalog.get_catalog_version", return_value="1.2.3"),
    ):
        result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schemaVersion"] == 2
    assert payload["isvTestVersion"] == "1.2.3"
    assert payload["entries"] == _FAKE_ENTRIES
    assert "kubernetes" in payload["capabilities"]
    assert "storage" in payload["suites"]


def test_catalog_labels_table() -> None:
    """`catalog labels` renders each label and its test count."""
    entries = [
        {"name": "A", "labels": ["iam", "security"]},
        {"name": "B", "labels": ["iam"]},
        {"name": "C", "labels": []},
    ]
    with patch("isvctl.cli.catalog.build_catalog", return_value=entries):
        result = runner.invoke(app, ["labels"])

    assert result.exit_code == 0, result.output
    assert "iam" in result.output
    assert "security" in result.output
    assert "Files" not in result.output


def test_catalog_labels_json_counts_tests_per_label() -> None:
    """`catalog labels --json` (default) emits sorted labels with test counts, no files."""
    entries = [
        {"name": "A", "labels": ["iam", "security"]},
        {"name": "B", "labels": ["iam"]},
        {"name": "C", "labels": []},
    ]
    with patch("isvctl.cli.catalog.build_catalog", return_value=entries):
        result = runner.invoke(app, ["labels", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["labels"] == [
        {"label": "iam", "tests": 2},
        {"label": "security", "tests": 1},
    ]


def test_catalog_labels_files_option_adds_files() -> None:
    """`catalog labels --files --json` includes the declaring config files per label."""
    entries = [
        {"name": "A", "labels": ["iam", "security"]},
        {"name": "B", "labels": ["iam"]},
        {"name": "C", "labels": []},
    ]
    file_map = {
        "iam": {"suites/control-plane.yaml", "suites/security.yaml"},
        "security": {"suites/security.yaml"},
    }
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=entries),
        patch("isvctl.cli.catalog.build_label_file_map", return_value=file_map),
    ):
        result = runner.invoke(app, ["labels", "--files", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["labels"] == [
        {
            "label": "iam",
            "tests": 2,
            "files": ["suites/control-plane.yaml", "suites/security.yaml"],
        },
        {"label": "security", "tests": 1, "files": ["suites/security.yaml"]},
    ]


def test_catalog_list_has_no_unreleased_mode() -> None:
    """A checkout always exposes its complete test set."""
    result = runner.invoke(app, ["list", "--unreleased"])
    assert result.exit_code != 0


@pytest.mark.parametrize("flag", ["--dry-run", "--no-upload"])
def test_catalog_push_dry_run_saves_without_upload(flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`catalog push --dry-run` / `--no-upload` saves locally and skips upload."""
    monkeypatch.setattr("isvctl.cli.catalog.get_output_dir", lambda: tmp_path)
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=_FAKE_ENTRIES),
        patch("isvctl.cli.catalog.get_catalog_version", return_value="1.2.3"),
        patch("isvtest.catalog.build_ref", return_value="v1.2.3-0-gdeadbee"),
        patch("isvctl.cli.catalog.build_is_release", return_value=True),
        patch("isvctl.reporting.check_upload_credentials") as check_creds,
    ):
        result = runner.invoke(app, ["push", flag])

    assert result.exit_code == 0, result.output
    check_creds.assert_not_called()
    catalog_path = tmp_path / "test_catalog.json"
    assert catalog_path.exists()
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert payload["entries"] == _FAKE_ENTRIES
    assert "Dry run: saved catalog locally" in result.output


def test_catalog_push_file_uploads_the_artifact_identity(tmp_path: Path) -> None:
    """Controlled publication reads, validates, and forwards one saved artifact."""
    artifact = {
        "schemaVersion": 2,
        "isvTestVersion": "1.2.3",
        "isvTestBuildRef": "v1.2.3-0-gdeadbee",
        "capabilities": ["kubernetes"],
        "suites": ["storage"],
        "entries": _FAKE_ENTRIES,
    }
    artifact["catalogDigest"] = catalog_digest(artifact)
    artifact_path = tmp_path / "release-catalog.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    with (
        patch("isvctl.cli.catalog.build_is_release", return_value=True),
        patch("isvctl.reporting.check_upload_credentials", return_value=(True, "id", "secret")),
        patch("isvctl.reporting.get_environment_config", return_value=("https://service", "https://ssa")),
        patch("isvreporter.auth.get_jwt_token", return_value="token"),
        patch("isvreporter.client.upload_test_catalog", return_value=True) as upload,
    ):
        result = runner.invoke(app, ["push", "--file", str(artifact_path)])

    assert result.exit_code == 0, result.output
    upload.assert_called_once_with(
        endpoint="https://service",
        jwt_token="token",
        isv_test_version="1.2.3",
        entries=_FAKE_ENTRIES,
        schema_version=2,
        capabilities=["kubernetes"],
        suites=["storage"],
        catalog_digest=artifact["catalogDigest"],
        isv_test_build_ref="v1.2.3-0-gdeadbee",
    )
