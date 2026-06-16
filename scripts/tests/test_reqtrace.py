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

"""Tests for reqtrace.py.

Acts as the CI drift guard: the committed requirements layer
(``test-plan.yaml`` + ``test-requirements-matrix.yaml`` +
``software-reference-requirements.md``) must stay internally consistent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "reqtrace.py"
_spec = importlib.util.spec_from_file_location("reqtrace", _SCRIPT)
assert _spec and _spec.loader
reqtrace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reqtrace)


def test_committed_requirements_layer_is_consistent() -> None:
    """`reqtrace validate` must pass against the committed files (no drift)."""
    assert reqtrace.validate() == 0


def test_sources_and_ids_resolve() -> None:
    """Sanity-check source discovery and id extraction."""
    ids = reqtrace.load_plan_test_ids(reqtrace.TEST_PLAN)
    assert "BOOT01-01" in ids

    sources = reqtrace.discover_sources()
    assert {"offtake", "reference"} <= set(sources)

    ref_ids, ref_dupes = reqtrace.source_req_ids(sources["reference"])
    assert "OBS03" in ref_ids and ref_dupes == []

    off_ids, _ = reqtrace.source_req_ids(sources["offtake"])
    assert "BOOT01" in off_ids


def test_coverage_runs() -> None:
    """`coverage` should execute cleanly."""
    assert reqtrace.coverage() == 0


def test_duplicate_source_name_is_rejected() -> None:
    """Two listings claiming the same `source` name must be flagged, not shadowed."""
    ok = [("offtake", Path("offtake-requirements.yaml")), ("reference", Path("software-reference-requirements.yaml"))]
    assert reqtrace.duplicate_source_name_errors(ok) == []

    clash = [
        ("reference", Path("software-reference-requirements.yaml")),
        ("reference", Path("team-x-requirements.yaml")),
    ]
    errs = reqtrace.duplicate_source_name_errors(clash)
    assert len(errs) == 1
    assert "reference" in errs[0]
    assert "software-reference-requirements.yaml" in errs[0]
    assert "team-x-requirements.yaml" in errs[0]


def test_incomplete_mapping_rows_are_rejected() -> None:
    """Empty or partial requirement edges must error, not silently count as mapped."""
    plan_ids = {"FOO01-01", "BAR01-01", "BAZ01-01"}
    source_sets = {"reference": {"FOO01"}}

    # A well-formed row resolves cleanly and counts as mapped.
    good = {"mappings": [{"test_id": "FOO01-01", "requirements": [{"req_id": "FOO01", "source": "reference"}]}]}
    errors, mapped_tests, mapped_reqs = reqtrace.mapping_row_errors(good, plan_ids, source_sets)
    assert errors == []
    assert mapped_tests == {"FOO01-01"} and mapped_reqs == {"FOO01"}

    # An empty `requirements` list must be an error, and must NOT count as covered.
    empty = {"mappings": [{"test_id": "BAR01-01", "requirements": []}]}
    errors, _, mapped_reqs = reqtrace.mapping_row_errors(empty, plan_ids, source_sets)
    assert any("no requirements" in e for e in errors)
    assert mapped_reqs == set()

    # A requirement missing req_id or source must be an error, not silently skipped.
    partial = {"mappings": [{"test_id": "BAZ01-01", "requirements": [{"source": "reference"}]}]}
    errors, _, mapped_reqs = reqtrace.mapping_row_errors(partial, plan_ids, source_sets)
    assert any("missing req_id/source" in e for e in errors)
    assert mapped_reqs == set()
