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

"""Tests for test_plan_coverage.py, including the CI drift guardrail."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "test_plan_coverage", Path(__file__).resolve().parent.parent / "test_plan_coverage.py"
)
assert _spec and _spec.loader
test_plan_coverage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_plan_coverage)


def test_integrity_errors_flags_unknown_test_id() -> None:
    """A class that declares a test_id missing from the plan is an error."""
    errors = test_plan_coverage.integrity_errors(
        plan_ids={"SEC01-01"},
        class_map={"GoodCheck": ["SEC01-01"], "BadCheck": ["NOPE-99"]},
    )
    assert len(errors) == 1
    assert "BadCheck" in errors[0]
    assert "NOPE-99" in errors[0]


def test_integrity_errors_empty_when_all_known() -> None:
    """No errors when every declared test_id exists in the plan."""
    assert test_plan_coverage.integrity_errors({"A-1", "B-2"}, {"C": ["A-1"], "D": ["B-2"]}) == []


def test_build_coverage_counts_covered_and_released() -> None:
    """Coverage counts plan items implemented by any class vs a released class."""
    plan = {
        "SEC01-01": {"req_id": "SEC01"},
        "SEC02-01": {"req_id": "SEC02"},
        "AUX-01": {"req_id": "AUX"},
    }
    class_map = {"ReleasedCheck": ["SEC01-01"], "UnreleasedCheck": ["SEC02-01"]}
    coverage = test_plan_coverage.build_coverage(plan, class_map, released={"ReleasedCheck"})

    assert coverage["plan_test_ids"] == 3
    assert coverage["plan_test_ids_covered"] == 2
    assert coverage["plan_test_ids_covered_by_released_class"] == 1


def test_real_test_ids_excludes_sentinel() -> None:
    """real_test_ids strips the UNMAPPED sentinel, leaving only plan ids."""
    assert test_plan_coverage.real_test_ids({"test_ids": ["SEC01-01", test_plan_coverage.UNMAPPED]}) == ["SEC01-01"]
    assert test_plan_coverage.real_test_ids({"test_ids": [test_plan_coverage.UNMAPPED]}) == []


def test_consistency_errors_flags_domain_mismatch() -> None:
    """A class whose labels don't match its test_id domain is flagged."""
    entries = [{"name": "WrongCheck", "labels": ["security"], "test_ids": ["K8S22-01"]}]
    errors = test_plan_coverage.consistency_errors(entries)
    assert len(errors) == 1
    assert "WrongCheck" in errors[0]


def test_consistency_errors_allows_cross_domain_and_unknown_prefix() -> None:
    """Cross-domain labels pass; prefixes without a rule are ignored."""
    entries = [
        {"name": "SgCheck", "labels": ["network", "security"], "test_ids": ["SDN02-05"]},
        {"name": "TenantCheck", "labels": ["iam"], "test_ids": ["CP-XX-07"]},  # CP has no rule
    ]
    assert test_plan_coverage.consistency_errors(entries) == []


def test_apply_config_labels_unions_yaml_labels_onto_entries() -> None:
    """YAML-wiring labels are unioned onto a catalog entry's existing labels."""
    entries = [{"name": "StepSuccessCheck", "labels": []}]
    merged = test_plan_coverage.apply_config_labels(entries, {"StepSuccessCheck": ["security"]})
    assert merged[0]["labels"] == ["security"]


def test_apply_config_labels_propagates_variant_labels_to_base() -> None:
    """A variant's YAML labels propagate up to its base class entry."""
    entries = [{"name": "SlurmPartition", "labels": []}]
    merged = test_plan_coverage.apply_config_labels(entries, {"SlurmPartition-gpu": ["slurm"]})
    assert merged[0]["labels"] == ["slurm"]


def test_consistency_passes_when_yaml_label_supplies_domain() -> None:
    """A SEC test_id on a label-less class passes once YAML supplies the label."""
    entries = [{"name": "StepSuccessCheck", "labels": [], "test_ids": ["SEC21-03"]}]
    assert test_plan_coverage.consistency_errors(entries)  # missing label -> error
    merged = test_plan_coverage.apply_config_labels(entries, {"StepSuccessCheck": ["security"]})
    assert test_plan_coverage.consistency_errors(merged) == []


def test_load_plan_rejects_duplicate_test_ids(tmp_path: Path) -> None:
    """A test_id repeated in the plan is fatal, not a silent last-write-wins."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "domains:\n"
        "  - components:\n"
        "      - capabilities:\n"
        "          - tests:\n"
        '              - {test_id: "SEC01-01", summary: first}\n'
        '              - {test_id: "SEC01-01", summary: second}\n'
    )
    with pytest.raises(SystemExit, match="SEC01-01"):
        test_plan_coverage.load_plan(plan)


def test_repo_metadata_passes_all_guardrails() -> None:
    """Guardrail: real metadata passes integrity and consistency.

    Fails loudly if a declared test_id drifts from docs/test-plan.yaml or a
    mapping's domain is inconsistent with the check's labels. There is no
    completeness check: a check with no test_id is allowed.
    """
    plan_ids = set(test_plan_coverage.load_plan())
    checks = test_plan_coverage.run_guardrails(plan_ids, test_plan_coverage.entries_from_config_maps())
    all_errors = [msg for msgs in checks.values() for msg in msgs]
    assert not all_errors, "\n  ".join(["test-plan coverage guardrails failed:", *all_errors])


def test_guardrail_fast_path_matches_catalog_path() -> None:
    """YAML-only guardrail seed must agree with the catalog-backed report path."""
    plan_ids = set(test_plan_coverage.load_plan())
    test_id_map = test_plan_coverage.config_test_id_map()
    label_map = test_plan_coverage.config_label_map()

    fast = test_plan_coverage.run_guardrails(
        plan_ids,
        test_plan_coverage.entries_from_config_maps(test_id_map, label_map),
    )
    slow = test_plan_coverage.run_guardrails(
        plan_ids,
        test_plan_coverage.entries_from_config_maps(
            test_id_map,
            label_map,
            test_plan_coverage.catalog_entries(),
        ),
    )
    assert fast == slow
