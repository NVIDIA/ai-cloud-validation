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


def _write_platform_axis_suites(suites_dir: Path) -> None:
    """Write vm + bare_metal platform suites so the derived platform axis is non-empty."""
    suites_dir.mkdir(parents=True, exist_ok=True)
    for platform in ("vm", "bare_metal"):
        (suites_dir / f"{platform}.yaml").write_text(
            f"""\
tests:
  platform: {platform}
  validations:
    example:
      checks:
        {platform.title().replace("_", "")}Check:
          test_id: "N/A"
          labels: ["{platform}"]
"""
        )


def test_wiring_errors_flags_platform_labels_on_module_suite_checks(tmp_path: Path) -> None:
    """Platform-axis names are banned from module-suite labels; platforms: is the mechanism."""
    _write_platform_axis_suites(tmp_path)
    (tmp_path / "security.yaml").write_text(
        """\
tests:
  module: security
  validations:
    example:
      checks:
        LabelledCheck:
          test_id: "SEC01-01"
          labels: ["bare_metal", "security"]
        DeclaredCheck:
          test_id: "SEC01-02"
          labels: ["security"]
          platforms: ["bare_metal"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any(
        "LabelledCheck" in err and "platform label(s) (bare_metal)" in err and "'platforms: [...]'" in err
        for err in errors
    )
    assert not any("DeclaredCheck" in err for err in errors)


def test_wiring_errors_accepts_platforms_subset_on_module_suite_checks(tmp_path: Path) -> None:
    """A multi-value platforms subset is legal on module-suite checks."""
    _write_platform_axis_suites(tmp_path)
    (tmp_path / "security.yaml").write_text(
        """\
tests:
  module: security
  validations:
    example:
      checks:
        SubsetCheck:
          test_id: "SEC01-01"
          labels: ["security"]
          platforms: ["vm", "bare_metal"]
"""
    )
    assert validate_suite_wiring.wiring_errors(tmp_path) == []


def test_wiring_errors_flags_unknown_platforms_value(tmp_path: Path) -> None:
    """Every platforms: value must be a member of the derived platform axis."""
    _write_platform_axis_suites(tmp_path)
    (tmp_path / "security.yaml").write_text(
        """\
tests:
  module: security
  validations:
    example:
      checks:
        TypoCheck:
          test_id: "SEC01-01"
          labels: ["security"]
          platforms: ["bare-metal"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("TypoCheck" in err and "unknown platform(s) in 'platforms': bare-metal" in err for err in errors)


def test_wiring_errors_flags_platforms_on_platform_suite_checks(tmp_path: Path) -> None:
    """platforms: is rejected in platform suites - the column is fixed by placement."""
    _write_platform_axis_suites(tmp_path)
    (tmp_path / "k8s.yaml").write_text(
        """\
tests:
  platform: kubernetes
  validations:
    example:
      checks:
        PinnedCheck:
          test_id: "K8S01-01"
          labels: ["kubernetes"]
          platforms: ["kubernetes"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("PinnedCheck" in err and "'platforms' is not allowed on platform-suite checks" in err for err in errors)


def test_wiring_errors_flags_unknown_platforms_value_in_provider_config(tmp_path: Path) -> None:
    """Provider config platforms: values are validated against the derived axis."""
    suites_dir = tmp_path / "suites"
    _write_platform_axis_suites(suites_dir)
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
          platforms: ["not_a_platform"]
"""
    )
    errors = validate_suite_wiring.wiring_errors(suites_dir, tmp_path / "providers")
    assert any("ProviderCheck" in err and "unknown platform(s) in 'platforms': not_a_platform" in err for err in errors)


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
