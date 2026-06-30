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
from typer.testing import CliRunner

import isvctl.cli.catalog as catalog_cli
from isvctl.cli.catalog import app

runner = CliRunner()

_FAKE_ENTRIES = [
    {
        "name": "AlphaCheck",
        "description": "Alpha description",
        "labels": ["kubernetes"],
        "module": "isvtest.validations.alpha",
        "platforms": ["KUBERNETES"],
    },
    {
        "name": "BetaCheck",
        "description": "",
        "labels": [],
        "module": "isvtest.validations.beta",
        "platforms": [],
    },
]


def _write_provider_config(root: Path, provider: str, name: str, suite: str) -> Path:
    """Write a provider config importing one suite."""
    config_path = root / "providers" / provider / "config" / name
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""\
import:
  - ../../../suites/{suite}
commands:
  demo:
    phases: [test]
    steps: []
tests:
  platform: demo
""",
        encoding="utf-8",
    )
    return config_path


def _write_suite(root: Path, name: str, labels: list[str], check_name: str) -> None:
    """Write a suite with one labelled validation check."""
    labels_yaml = ", ".join(f'"{label}"' for label in labels)
    suite_path = root / "suites" / name
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(
        f"""\
tests:
  validations:
    sample:
      checks:
        {check_name}:
          test_id: "N/A"
          labels: [{labels_yaml}]
""",
        encoding="utf-8",
    )


def test_catalog_help() -> None:
    """Top-level catalog help mentions the new list command."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "list" in result.output


def test_catalog_list_table() -> None:
    """`catalog list` renders a table containing the discovered tests."""
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=_FAKE_ENTRIES),
        patch("isvctl.cli.catalog.get_catalog_version", return_value="1.2.3"),
    ):
        result = runner.invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert "AlphaCheck" in result.output
    assert "BetaCheck" in result.output
    assert "1.2.3" in result.output


def test_catalog_list_json() -> None:
    """`catalog list --json` emits parseable JSON matching the saved artifact shape."""
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=_FAKE_ENTRIES),
        patch("isvctl.cli.catalog.get_catalog_version", return_value="1.2.3"),
    ):
        result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["isvTestVersion"] == "1.2.3"
    assert payload["entries"] == _FAKE_ENTRIES


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


def test_catalog_labels_provider_json_honors_release_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`catalog labels --provider --json` lists runnable provider labels only."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "vm.yaml", ["vm"], "VmCheck")
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_suite(configs_root, "observability.yaml", ["network", "observability"], "VpcFlowLogsCheck")
    _write_suite(configs_root, "gpu.yaml", ["gpu"], "GpuCheck")
    _write_suite(configs_root, "future.yaml", ["future"], "UnreleasedCheck")
    _write_provider_config(configs_root, "aws", "vm.yaml", "vm.yaml")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml")
    _write_provider_config(configs_root, "aws", "observability.yaml", "observability.yaml")
    _write_provider_config(configs_root, "aws", "gpu.yaml", "gpu.yaml")
    _write_provider_config(configs_root, "aws", "future.yaml", "future.yaml")
    monkeypatch.setattr(catalog_cli, "CONFIGS_ROOT", configs_root)
    monkeypatch.setattr(
        catalog_cli,
        "load_released_test_filter",
        lambda: {"VmCheck", "NetworkCheck", "VpcFlowLogsCheck", "GpuCheck"},
    )

    result = runner.invoke(app, ["labels", "--provider", "aws", "--files", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["provider"] == "aws"
    assert payload["labels"] == [
        {
            "label": "vm",
            "display": "VMaaS",
            "kind": "capability",
            "tests": 1,
            "configs": 1,
            "files": ["providers/aws/config/vm.yaml"],
        },
        {
            "label": "network",
            "display": "Networking",
            "kind": "requirement",
            "tests": 2,
            "configs": 2,
            "files": [
                "providers/aws/config/network.yaml",
                "providers/aws/config/observability.yaml",
            ],
        },
        {
            "label": "observability",
            "display": "Observability",
            "kind": "requirement",
            "tests": 1,
            "configs": 1,
            "files": ["providers/aws/config/observability.yaml"],
        },
        {
            "label": "gpu",
            "display": "GPU",
            "kind": "trait",
            "tests": 1,
            "configs": 1,
            "files": ["providers/aws/config/gpu.yaml"],
        },
    ]


def test_catalog_labels_provider_unknown_lists_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unknown provider reports discoverable providers for label inspection."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml")
    monkeypatch.setattr(catalog_cli, "CONFIGS_ROOT", configs_root)

    result = runner.invoke(app, ["labels", "--provider", "gcp", "--json"])

    assert result.exit_code == 1, result.output
    assert "Unknown provider 'gcp'" in result.output
    assert "aws" in result.output


def test_catalog_list_unreleased_json() -> None:
    """`catalog list --unreleased` emits only entries missing from the release manifest."""
    with (
        patch("isvctl.cli.catalog.build_catalog", return_value=_FAKE_ENTRIES) as build_catalog,
        patch("isvctl.cli.catalog.load_released_tests", return_value={"AlphaCheck"}),
        patch("isvctl.cli.catalog.get_catalog_version", return_value="1.2.3"),
    ):
        result = runner.invoke(app, ["list", "--unreleased", "--json"])

    assert result.exit_code == 0, result.output
    build_catalog.assert_called_once_with(released_only=False)
    payload = json.loads(result.output)
    assert payload["entries"] == [_FAKE_ENTRIES[1]]
