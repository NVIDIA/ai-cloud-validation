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
          labels: ["demo", "security"]
          requires: []
        BadCheck:
          labels: ["demo", "security"]
          requires: []
        AlsoBad:
          test_id: "N/A"
          requires: []
"""
    )
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("demo.yaml:" in err and "BadCheck" in err and "missing test_id" in err for err in errors)
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


def test_wiring_errors_reports_yaml_parse_failures(tmp_path: Path) -> None:
    """Malformed suite YAML surfaces as a validation error instead of being skipped."""
    suite = tmp_path / "broken.yaml"
    suite.write_text("tests:\n  validations:\n    bad: [:\n")
    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert len(errors) == 1
    assert "broken.yaml" in errors[0]
    assert "failed to read/parse" in errors[0]


def test_wiring_errors_scans_nested_suite_directories(tmp_path: Path) -> None:
    """Domain-organized suites receive the same metadata guardrails as root suites."""
    nested = tmp_path / "launch-kit"
    nested.mkdir()
    (nested / "network-operator.yaml").write_text(
        "tests:\n  validations:\n    sample:\n      checks:\n        MissingMetadata: {}\n"
    )

    errors = validate_suite_wiring.wiring_errors(tmp_path)

    assert any("launch-kit/network-operator.yaml" in error and "MissingMetadata" in error for error in errors)


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


def test_repo_provider_configs_resolve_after_merging() -> None:
    """Guardrail: provider overrides preserve composites and never wire generics directly."""
    errors = validate_suite_wiring.provider_wiring_errors()
    assert not errors, "provider wiring validation failed:\n  " + "\n  ".join(errors)


def test_plain_suite_requires_are_explicit_and_valid(tmp_path: Path) -> None:
    """Plain suites require an allowed list, including an explicit empty list."""
    (tmp_path / "demo.yaml").write_text(
        """\
tests:
  validations:
    sample:
      checks:
        MissingCheck:
          test_id: "N/A"
          labels: ["demo"]
        InvalidCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: [foundational]
"""
    )

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("MissingCheck" in error and "missing requires" in error for error in errors)
    assert any("InvalidCheck" in error and "requires must be a list containing only" in error for error in errors)


def test_wiring_errors_rejects_plain_suite_named_after_capability(tmp_path: Path) -> None:
    """A plain suite file named like a declarable capability is a namespace collision."""
    (tmp_path / "kubernetes.yaml").write_text(
        """\
tests:
  validations:
    sample:
      checks:
        SomeCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
"""
    )

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("kubernetes" in error and "collides with a declarable capability" in error for error in errors)


def test_wiring_errors_flags_requires_with_no_platform_suite(tmp_path: Path) -> None:
    """A `requires` naming a capability that has no platform suite is unreachable."""
    (tmp_path / "vm.yaml").write_text(
        """\
tests:
  capability: vm
  validations:
    example:
      checks:
        VmCheck:
          test_id: "N/A"
          labels: ["vm"]
"""
    )
    (tmp_path / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      checks:
        DeadCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: [slurm]
"""
    )

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("DeadCheck" in error and "slurm" in error and "no platform suite" in error for error in errors)


def test_wiring_errors_accepts_a_well_formed_composite(tmp_path: Path) -> None:
    """A composite naming real checks and describing itself is valid wiring."""
    (tmp_path / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      step: create_user
      checks:
        DemoUserCreatedCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
          description: "Check the demo user is created"
          compose:
            - StepSuccessCheck
            - FieldExistsCheck:
                fields: ["username"]
"""
    )

    assert validate_suite_wiring.wiring_errors(tmp_path) == []


def test_wiring_errors_flags_composite_problems(tmp_path: Path) -> None:
    """A composite must describe itself, name real checks, and own its name."""
    (tmp_path / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      step: create_user
      checks:
        NoDescriptionCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
          compose:
            - StepSuccessCheck
        UnknownMemberCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
          description: "Check something"
          compose:
            - NoSuchCheck
        StepSuccessCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
          description: "Check something"
          compose:
            - StepSuccessCheck
        EmptyComposeCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
          description: "Check something"
          compose: []
"""
    )

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("NoDescriptionCheck" in err and "requires a description" in err for err in errors)
    assert any("UnknownMemberCheck" in err and "unknown check 'NoSuchCheck'" in err for err in errors)
    assert any("StepSuccessCheck" in err and "shadows validation class" in err for err in errors)
    assert any("EmptyComposeCheck" in err and "non-empty list" in err for err in errors)


def test_wiring_errors_flags_malformed_compose_items(tmp_path: Path) -> None:
    """A ``compose`` item in neither supported form would silently be dropped."""
    (tmp_path / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      step: create_user
      checks:
        MalformedComposeCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
          description: "Check something"
          compose:
            - StepSuccessCheck
            - [FieldExistsCheck]
"""
    )

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("MalformedComposeCheck" in err and "must be 'CheckName'" in err for err in errors)


def _generic_wiring_suite(tmp_path: Path) -> None:
    """Write a suite that wires a generic check directly and as a variant."""
    (tmp_path / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      step: create_user
      checks:
        StepSuccessCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
        StepSuccessCheck-teardown:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
"""
    )


def test_wiring_names_must_be_unique_across_suites(tmp_path: Path) -> None:
    """The catalog keys on the wiring name, so a reused name would drop a test."""
    for suite in ("demo", "other"):
        (tmp_path / f"{suite}.yaml").write_text(
            f"""\
tests:
  validations:
    example:
      step: create_user
      checks:
        DemoUserCreatedCheck:
          test_id: "N/A"
          labels: ["{suite}"]
          requires: []
          description: "Check the demo user is created"
          compose:
            - StepSuccessCheck
"""
        )

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("DemoUserCreatedCheck" in err and "not globally unique" in err for err in errors)


def test_generic_checks_may_not_be_wired_directly(tmp_path: Path) -> None:
    """A compose_only check wired under its class name is rejected."""
    _generic_wiring_suite(tmp_path)

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert sum("may only appear in a composite" in err for err in errors) == 2


def test_generic_check_variant_names_are_also_rejected(tmp_path: Path) -> None:
    """Suffixing a generic class name does not make it a name of its own."""
    _generic_wiring_suite(tmp_path)

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert any("StepSuccessCheck-teardown" in err and "may only appear in a composite" in err for err in errors)


def test_generic_checks_are_allowed_inside_a_composite(tmp_path: Path) -> None:
    """``compose`` is exactly where a generic check belongs."""
    (tmp_path / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      step: create_user
      checks:
        DemoUserCreatedCheck:
          test_id: "N/A"
          labels: ["demo"]
          requires: []
          description: "Check the demo user is created"
          compose:
            - StepSuccessCheck
            - FieldExistsCheck:
                fields: ["username"]
"""
    )

    assert validate_suite_wiring.wiring_errors(tmp_path) == []


def test_wiring_errors_allows_platform_suite_named_after_capability(tmp_path: Path) -> None:
    """The kubernetes *platform* suite (declares tests.capability) is not a collision."""
    (tmp_path / "k8s.yaml").write_text(
        """\
tests:
  capability: kubernetes
  validations:
    sample:
      checks:
        SomeCheck:
          test_id: "N/A"
          labels: ["kubernetes"]
"""
    )

    errors = validate_suite_wiring.wiring_errors(tmp_path)
    assert not any("collides with a declarable capability" in error for error in errors)


def test_provider_wiring_errors_checks_the_merged_override(tmp_path: Path) -> None:
    """A provider override cannot strip an imported composite down to an unknown name."""
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        """\
tests:
  validations:
    example:
      checks:
        DemoCreatedCheck:
          description: "Check the demo is created"
          compose:
            - StepSuccessCheck
"""
    )
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "demo.yaml").write_text(
        f"""\
import:
  - {suite}
tests:
  validations:
    example:
      checks:
        - DemoCreatedCheck: {{}}
"""
    )

    errors = validate_suite_wiring.provider_wiring_errors(providers)
    assert any("DemoCreatedCheck" in error and "unknown validation" in error for error in errors)


def test_provider_wiring_errors_rejects_provider_only_generic_checks(tmp_path: Path) -> None:
    """Provider-only configs receive the same compose-only protection as suites."""
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      checks:
        StepSuccessCheck: {}
"""
    )

    errors = validate_suite_wiring.provider_wiring_errors(providers)
    assert any("StepSuccessCheck" in error and "may only appear in a composite" in error for error in errors)


def test_provider_wiring_errors_accepts_provider_only_composites(tmp_path: Path) -> None:
    """A named provider-only composite is valid without suite-owned labels."""
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "demo.yaml").write_text(
        """\
tests:
  validations:
    example:
      checks:
        DemoCreatedCheck:
          description: "Check the demo is created"
          compose:
            - StepSuccessCheck
"""
    )

    assert validate_suite_wiring.provider_wiring_errors(providers) == []
