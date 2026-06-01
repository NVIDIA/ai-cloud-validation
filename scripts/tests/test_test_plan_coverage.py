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


def test_build_coverage_counts_min_req_covered_by_released() -> None:
    """min_req coverage only counts test IDs implemented by a released class."""
    plan = {
        "SEC01-01": {"req_id": "SEC01", "labels": ["min_req"]},
        "SEC02-01": {"req_id": "SEC02", "labels": ["min_req"]},
        "AUX-01": {"req_id": "AUX", "labels": []},
    }
    class_map = {"ReleasedCheck": ["SEC01-01"], "UnreleasedCheck": ["SEC02-01"]}
    coverage = test_plan_coverage.build_coverage(plan, class_map, released={"ReleasedCheck"})

    assert coverage["min_req_test_ids"] == 2
    assert coverage["min_req_covered_by_released_class"] == 1
    assert coverage["min_req_uncovered"] == ["SEC02-01"]


def test_repo_class_metadata_references_only_known_test_ids() -> None:
    """Guardrail: every test_ids value declared in code must exist in the plan.

    This fails loudly when a class's test_ids drifts from docs/test-plan.yaml
    (typo, renamed/removed test_id), keeping the code<->plan link honest.
    """
    plan_ids = set(test_plan_coverage.load_plan())
    class_map = test_plan_coverage.class_test_id_map()
    errors = test_plan_coverage.integrity_errors(plan_ids, class_map)
    assert not errors, "Stale test_ids references:\n  " + "\n  ".join(errors)
