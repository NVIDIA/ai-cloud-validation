# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for validate_suite_wiring.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "validate_suite_wiring", Path(__file__).resolve().parent.parent / "validate_suite_wiring.py"
)
assert _spec and _spec.loader
validate_suite_wiring = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_suite_wiring)


def test_wiring_errors_flags_missing_metadata(tmp_path: Path) -> None:
    """Missing test_id or labels on a wired check is reported with context."""
    suite = tmp_path / "demo.yaml"
    suite.write_text(
        """\
tests:
  module: security
  validations:
    example:
      checks:
        GoodCheck:
          test_id: "SEC01-01"
          labels: ["security"]
        BadCheck:
          labels: ["security"]
        AlsoBad:
          test_id: "N/A"
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("demo.yaml:9" in err and "BadCheck" in err and "missing test_id" in err for err in errors)
    assert any("demo.yaml:" in err and "AlsoBad" in err and "missing labels" in err for err in errors)
    assert not any("GoodCheck" in err for err in errors)


def test_wiring_errors_rejects_scalar_labels(tmp_path: Path) -> None:
    """Scalar ``labels`` values must fail validation; only lists are accepted."""
    suite = tmp_path / "demo.yaml"
    suite.write_text(
        """\
tests:
  capability: kubernetes
  validations:
    example:
      checks:
        BadCheck:
          test_id: "N/A"
          labels: kubernetes
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("BadCheck" in err and "missing labels" in err for err in errors)


def test_wiring_errors_require_canonical_suite_label(tmp_path: Path) -> None:
    """Checks in known suite files must include that suite's label."""
    suite = tmp_path / "k8s.yaml"
    suite.write_text(
        """\
tests:
  capability: kubernetes
  validations:
    example:
      checks:
        MissingSuiteLabel:
          test_id: "K8S01-01"
          labels: ["gpu"]
        GoodCheck:
          test_id: "K8S01-02"
          labels: ["gpu", "kubernetes"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("MissingSuiteLabel" in err and "missing suite label 'kubernetes'" in err for err in errors)
    assert not any("GoodCheck" in err for err in errors)


def test_wiring_errors_flags_missing_or_conflicting_axis_key(tmp_path: Path) -> None:
    """A suite must declare exactly one of tests.capability / tests.module."""
    (tmp_path / "no_axis.yaml").write_text(
        """\
tests:
  platform: vm
  validations:
    example:
      checks:
        GoodCheck:
          test_id: "VM01-01"
          labels: ["vm"]
"""
    )
    (tmp_path / "both_axes.yaml").write_text(
        """\
tests:
  capability: vm
  module: iam
  validations:
    example:
      checks:
        GoodCheck:
          test_id: "VM01-02"
          labels: ["vm"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("no_axis.yaml" in err and "missing axis key" in err for err in errors)
    assert any("both_axes.yaml" in err and "both tests.capability and tests.module" in err for err in errors)


def test_wiring_errors_flags_unknown_label(tmp_path: Path) -> None:
    """A typo'd label that is neither capability, module, nor modifier fails."""
    suite = tmp_path / "network.yaml"
    suite.write_text(
        """\
tests:
  module: network
  validations:
    example:
      checks:
        TypoCheck:
          test_id: "NET01-01"
          labels: ["network", "netwrok"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("TypoCheck" in err and "unknown label 'netwrok'" in err for err in errors)


def test_wiring_errors_flags_multiple_capability_labels(tmp_path: Path) -> None:
    """A check may carry at most one capability-axis label."""
    (tmp_path / "bare_metal.yaml").write_text(
        """\
tests:
  capability: bare_metal
  validations:
    example:
      checks:
        BmOnly:
          test_id: "BM01-01"
          labels: ["bare_metal"]
"""
    )
    (tmp_path / "vm.yaml").write_text(
        """\
tests:
  capability: vm
  validations:
    example:
      checks:
        DualCapability:
          test_id: "VM01-01"
          labels: ["vm", "bare_metal"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any(
        "DualCapability" in err and "multiple capability labels" in err and "bare_metal, vm" in err for err in errors
    )


def test_wiring_errors_flags_provider_config_label_typo(tmp_path: Path) -> None:
    """Provider configs are governed for labels even though they inherit the axis key."""
    suites_dir = tmp_path / "suites"
    suites_dir.mkdir()
    (suites_dir / "network.yaml").write_text(
        """\
tests:
  module: network
  validations:
    example:
      checks:
        GoodCheck:
          test_id: "NET01-01"
          labels: ["network"]
"""
    )
    provider_config = tmp_path / "providers" / "acme" / "config" / "network.yaml"
    provider_config.parent.mkdir(parents=True)
    provider_config.write_text(
        """\
tests:
  validations:
    example:
      checks:
        ProviderCheck:
          test_id: "NET01-02"
          labels: ["netwrok"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(suites_dir, tmp_path / "providers")
    assert any("ProviderCheck" in err and "unknown label 'netwrok'" in err for err in errors)


def test_derive_axis_labels_covers_platform_labels() -> None:
    """Guardrail: derived axis labels cover every catalog platform-ownership label."""
    from isvtest.catalog import LABEL_TO_PLATFORM

    capability_labels, module_labels = validate_suite_wiring.derive_axis_labels()
    assert set(LABEL_TO_PLATFORM).issubset(capability_labels | module_labels)


def test_wiring_errors_reports_yaml_parse_failures(tmp_path: Path) -> None:
    """Malformed suite YAML surfaces as a validation error instead of being skipped."""
    suite = tmp_path / "broken.yaml"
    suite.write_text("tests:\n  validations:\n    bad: [:\n")
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert len(errors) == 1
    assert "broken.yaml" in errors[0]
    assert "failed to read/parse" in errors[0]


def test_find_check_line_numbers_supports_list_form() -> None:
    """List-form wiring reports each repeated check at its own line."""
    lines = """
tests:
  validations:
    pools:
      - K8sNodePoolCheck:
          test_id: "K8S06-01"
          labels: ["kubernetes"]
      - K8sNodePoolCheck:
          labels: ["kubernetes"]
""".splitlines()
    assert validate_suite_wiring.find_check_line_numbers(lines, "pools", "K8sNodePoolCheck") == [5, 8]


def test_repo_suites_declare_test_id_and_labels() -> None:
    """Guardrail: every check in isvctl/configs/suites declares wiring metadata."""
    errors = validate_suite_wiring.wiring_errors()
    assert not errors, "suite wiring validation failed:\n  " + "\n  ".join(errors)
