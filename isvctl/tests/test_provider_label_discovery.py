# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for provider-scoped label discovery."""

from __future__ import annotations

from pathlib import Path

from isvctl.config.label_discovery import available_labels, discover_provider_label_configs


def _write_provider_config(root: Path, provider: str, name: str, suite: str) -> Path:
    """Write a provider config importing one provider-neutral suite."""
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


def test_discovers_provider_configs_matching_label_through_imports(tmp_path: Path) -> None:
    """Provider label discovery matches every provider config whose resolved imports contain the label."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_suite(configs_root, "observability.yaml", ["network", "observability"], "VpcFlowLogsCheck")
    _write_suite(configs_root, "iam.yaml", ["iam"], "IamCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml")
    _write_provider_config(configs_root, "aws", "observability.yaml", "observability.yaml")
    _write_provider_config(configs_root, "aws", "iam.yaml", "iam.yaml")

    matches = discover_provider_label_configs("aws", ["network"], configs_root=configs_root)

    assert [match.config_path.name for match in matches] == ["network.yaml", "observability.yaml"]
    assert [check.name for check in matches[0].matched_checks] == ["NetworkCheck"]
    assert [check.name for check in matches[1].matched_checks] == ["VpcFlowLogsCheck"]


def test_discovery_excludes_configs_matched_only_by_unreleased_checks(tmp_path: Path) -> None:
    """A config selected solely on an unreleased check is dropped when the release filter is active."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_suite(configs_root, "observability.yaml", ["network"], "UnreleasedCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml")
    _write_provider_config(configs_root, "aws", "observability.yaml", "observability.yaml")

    matches = discover_provider_label_configs(
        "aws", ["network"], configs_root=configs_root, released_tests={"NetworkCheck"}
    )

    assert [match.config_path.name for match in matches] == ["network.yaml"]
    assert [check.name for check in matches[0].matched_checks] == ["NetworkCheck"]


def test_available_labels_honors_release_filter(tmp_path: Path) -> None:
    """Provider label suggestions only include labels from checks that can run."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_suite(configs_root, "future.yaml", ["future"], "UnreleasedCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml")
    _write_provider_config(configs_root, "aws", "future.yaml", "future.yaml")

    labels = available_labels("aws", configs_root=configs_root, released_tests={"NetworkCheck"})

    assert labels == {"network"}


def test_discovery_requires_all_requested_labels(tmp_path: Path) -> None:
    """Repeated labels use AND semantics, matching the existing runtime label filter."""
    configs_root = tmp_path / "configs"
    _write_suite(configs_root, "network.yaml", ["network"], "NetworkCheck")
    _write_suite(configs_root, "observability.yaml", ["network", "observability"], "VpcFlowLogsCheck")
    _write_provider_config(configs_root, "aws", "network.yaml", "network.yaml")
    _write_provider_config(configs_root, "aws", "observability.yaml", "observability.yaml")

    matches = discover_provider_label_configs("aws", ["network", "observability"], configs_root=configs_root)

    assert [match.config_path.name for match in matches] == ["observability.yaml"]
    assert [check.name for check in matches[0].matched_checks] == ["VpcFlowLogsCheck"]
