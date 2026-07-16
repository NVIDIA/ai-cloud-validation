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
    assert any("demo.yaml:8" in err and "BadCheck" in err and "missing test_id" in err for err in errors)
    assert any("demo.yaml:" in err and "AlsoBad" in err and "missing labels" in err for err in errors)
    assert not any("GoodCheck" in err for err in errors)


def test_wiring_errors_rejects_scalar_labels(tmp_path: Path) -> None:
    """Scalar ``labels`` values must fail validation; only lists are accepted."""
    suite = tmp_path / "demo.yaml"
    suite.write_text(
        """\
tests:
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


def test_platform_registration_errors_flags_unregistered_suite(tmp_path: Path) -> None:
    """A suite file not present in PLATFORM_CONFIGS is reported.

    Enforced against the real repo only by the validate-suites pre-commit
    hook, not by a repo-level pytest guard, so untracked scratch suites
    don't break `make test`.
    """
    (tmp_path / "newcap.yaml").write_text("tests:\n  validations: {}\n")
    errors = validate_suite_wiring.platform_registration_errors(tmp_path)
    assert errors == [
        "suites/newcap.yaml: not registered in isvtest.catalog_platforms.PLATFORM_CONFIGS (catalog platform axis)"
    ]


def test_registered_platforms_round_trip_through_isvreporter() -> None:
    """Guardrail: every catalog platform is recognized by isvreporter."""
    errors = validate_suite_wiring.registry_consistency_errors()
    assert not errors, "registry consistency failed:\n  " + "\n  ".join(errors)
