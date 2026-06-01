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


def test_completeness_errors_flags_unlisted_released_class() -> None:
    """A released class with no test_ids that isn't allow-listed is an error."""
    entries = [
        {"name": "MappedCheck", "labels": ["security"], "test_ids": ["SEC01-01"]},
        {"name": "ForgotCheck", "labels": ["security"], "test_ids": []},
        {"name": "UnreleasedCheck", "labels": [], "test_ids": []},
    ]
    errors = test_plan_coverage.completeness_errors(entries, released={"MappedCheck", "ForgotCheck"})
    assert len(errors) == 1
    assert "ForgotCheck" in errors[0]


def test_completeness_errors_respects_allowlist_and_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow-listed names and unreleased classes do not trigger completeness errors."""
    monkeypatch.setattr(test_plan_coverage, "ALLOWLIST_UNMAPPED", frozenset({"GenericCheck"}))
    entries = [
        {"name": "GenericCheck", "labels": [], "test_ids": []},  # allow-listed
        {"name": "DraftCheck", "labels": [], "test_ids": []},  # not released
    ]
    assert test_plan_coverage.completeness_errors(entries, released={"GenericCheck"}) == []


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


def test_repo_metadata_passes_all_guardrails() -> None:
    """Guardrail: real class metadata passes integrity, completeness, and consistency.

    Fails loudly if a class's test_ids drift from docs/test-plan.yaml, a released
    class is missing test_ids without being allow-listed, or a mapping's domain
    is inconsistent with the class labels.
    """
    plan_ids = set(test_plan_coverage.load_plan())
    entries = test_plan_coverage.catalog_entries()
    class_map = test_plan_coverage.class_test_id_map(entries)
    released = test_plan_coverage.released_names()

    integrity = test_plan_coverage.integrity_errors(plan_ids, class_map)
    completeness = test_plan_coverage.completeness_errors(entries, released)
    consistency = test_plan_coverage.consistency_errors(entries)
    assert not (integrity or completeness or consistency), "\n  ".join(
        ["test-plan coverage guardrails failed:", *integrity, *completeness, *consistency]
    )
