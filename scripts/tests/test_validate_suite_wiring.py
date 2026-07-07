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
  platform: kubernetes
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


def test_wiring_errors_require_declared_suite_label(tmp_path: Path) -> None:
    """Checks must include the suite label derived from tests.platform/module."""
    suite = tmp_path / "custom-name.yaml"
    suite.write_text(
        """\
tests:
  module: custom_module
  validations:
    example:
      checks:
        MissingSuiteLabel:
          test_id: "MOD01-01"
          labels: ["gpu"]
        GoodCheck:
          test_id: "MOD01-02"
          labels: ["gpu", "custom_module"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("MissingSuiteLabel" in err and "missing suite label 'custom_module'" in err for err in errors)
    assert not any("GoodCheck" in err for err in errors)


def test_wiring_errors_flags_missing_or_conflicting_axis_key(tmp_path: Path) -> None:
    """A suite must declare exactly one of tests.platform / tests.module."""
    (tmp_path / "no_axis.yaml").write_text(
        """\
tests:
  cluster_name: no-axis
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
  platform: vm
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
    assert any("both_axes.yaml" in err and "both tests.platform and tests.module" in err for err in errors)


def test_wiring_errors_flags_multiple_platform_labels(tmp_path: Path) -> None:
    """A check may carry at most one platform-axis label."""
    (tmp_path / "bare_metal.yaml").write_text(
        """\
tests:
  platform: bare_metal
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
  platform: vm
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
        "DualCapability" in err and "multiple platform labels" in err and "bare_metal, vm" in err for err in errors
    )


def test_wiring_errors_flags_provider_config_multiple_platform_labels(tmp_path: Path) -> None:
    """Provider configs are governed for labels even though they inherit the axis key."""
    suites_dir = tmp_path / "suites"
    suites_dir.mkdir()
    (suites_dir / "vm.yaml").write_text(
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
    (suites_dir / "bare_metal.yaml").write_text(
        """\
tests:
  platform: bare_metal
  validations:
    example:
      checks:
        GoodCheck:
          test_id: "BM01-01"
          labels: ["bare_metal"]
"""
    )
    provider_config = tmp_path / "providers" / "acme" / "config" / "vm.yaml"
    provider_config.parent.mkdir(parents=True)
    provider_config.write_text(
        """\
tests:
  validations:
    example:
      checks:
        ProviderCheck:
          test_id: "VM01-02"
          labels: ["vm", "bare_metal"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(suites_dir, tmp_path / "providers")
    assert any("ProviderCheck" in err and "multiple platform labels" in err for err in errors)


def test_derive_axis_labels_match_catalog_taxonomy() -> None:
    """Guardrail: wiring validator and catalog share the same axis scanner."""
    from isvtest.catalog import build_axis_taxonomy

    platform_labels, module_labels = validate_suite_wiring.derive_axis_labels()
    tax_platforms, tax_modules = build_axis_taxonomy()
    assert platform_labels == frozenset(tax_platforms)
    assert module_labels == frozenset(tax_modules)


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


def test_wiring_errors_flags_duplicate_names_within_provider_config(tmp_path: Path) -> None:
    """Provider wiring names must be unique within each provider config file."""
    suites_dir = tmp_path / "suites"
    suites_dir.mkdir()
    provider_config = tmp_path / "providers" / "acme" / "config" / "network.yaml"
    provider_config.parent.mkdir(parents=True)
    provider_config.write_text(
        """\
tests:
  validations:
    network_connectivity:
      checks:
        FieldValueCheck:
          test_id: "N/A"
          field: "tests.network_assigned.passed"
          expected: true
    traffic_validation:
      checks:
        FieldValueCheck:
          test_id: "N/A"
          field: "tests.network_setup.passed"
          expected: true
"""
    )
    errors = validate_suite_wiring.wiring_errors(suites_dir, tmp_path / "providers")
    assert any("network.yaml" in err and "FieldValueCheck" in err and "appears more than once" in err for err in errors)
    assert any("must use a variant name" in err for err in errors)


def test_wiring_errors_flags_bare_variant_required_class(tmp_path: Path) -> None:
    """Reusable generic checks must be wired with a variant suffix."""
    suite = tmp_path / "demo.yaml"
    suite.write_text(
        """\
tests:
  module: control_plane
  validations:
    api_health:
      checks:
        FieldValueCheck:
          test_id: "N/A"
          labels: ["control_plane"]
          field: "success"
          expected: true
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("FieldValueCheck" in err and "must use a variant name" in err for err in errors)
