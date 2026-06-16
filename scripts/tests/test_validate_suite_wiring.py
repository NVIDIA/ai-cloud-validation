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
