# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for isvtest.main module."""

import isvtest.main
from isvtest.core.resolution import ErrorReason, ResolvedEntry, SkipReason, State, ValidationEntry
from isvtest.main import (
    _entries_with_pytest_names,
    _resolved_entries_to_pytest_validations,
    run_validations_via_pytest,
)


def test_dummy() -> None:
    """A simple dummy test that always passes."""
    assert True


def test_main_module_exists() -> None:
    """Test that the main module can be imported."""
    assert isvtest.main is not None


def _ready(name: str, category: str, params: dict[str, object]) -> ResolvedEntry:
    """Build a ready resolved entry for main-module tests."""
    return ResolvedEntry(
        entry=ValidationEntry(name=name, category=category, params_template={}),
        rendered_params={**params, "_category": category},
    )


def _keys(result: list[dict[str, dict[str, object]]]) -> list[str]:
    """Extract validation keys from pytest config entries."""
    return [next(iter(entry)) for entry in result]


def test_resolved_entries_to_pytest_validations_keeps_unique_names() -> None:
    """Unique ready entries keep their configured validation names."""
    result = _resolved_entries_to_pytest_validations(
        _entries_with_pytest_names(
            [
                _ready("StepSuccessCheck", "setup_checks", {"step_output": {"success": True}}),
                _ready("FieldExistsCheck", "setup_checks", {"step_output": {"id": "x"}, "field": "id"}),
            ]
        )
    )

    assert _keys(result) == ["StepSuccessCheck", "FieldExistsCheck"]


def test_resolved_entries_to_pytest_validations_disambiguates_duplicates() -> None:
    """Duplicate ready entries receive category and counter suffixes."""
    result = _resolved_entries_to_pytest_validations(
        _entries_with_pytest_names(
            [
                _ready("StepSuccessCheck", "setup_checks", {"step_output": {"success": True}}),
                _ready("StepSuccessCheck", "teardown_checks", {"step_output": {"success": True}}),
                _ready("StepSuccessCheck", "teardown_checks", {"step_output": {"success": True}}),
            ]
        )
    )

    assert _keys(result) == [
        "StepSuccessCheck-setup_checks",
        "StepSuccessCheck-teardown_checks",
        "StepSuccessCheck-teardown_checks-2",
    ]


def test_run_validations_via_pytest_updates_ready_entries() -> None:
    """The pytest bridge returns terminal states on the same resolved-entry channel."""
    entries = [
        _ready("StepSuccessCheck", "setup_checks", {"step_output": {"success": True, "message": "ok"}}),
        _ready(
            "NimHealthCheck",
            "nim",
            {"step_output": {"skipped": True, "skip_reason": "NIM was not deployed"}},
        ),
    ]

    exit_code, results = run_validations_via_pytest(entries=entries)

    assert exit_code == 0
    assert [result.state for result in results] == [State.PASSED, State.SKIPPED]
    assert results[0].message == "ok"
    assert results[1].skip_reason == SkipReason.RUNTIME_SKIP
    assert results[1].message == "NIM was not deployed"


def test_run_validations_via_pytest_does_not_rerender_resolved_config_strings() -> None:
    """Resolved temp configs may contain literal Jinja-looking strings from step output."""
    jsonpath_probe = "kubectl get pods -o jsonpath='{{\"\\t\"}}'"
    entries = [
        _ready(
            "StepSuccessCheck",
            "setup_checks",
            {"step_output": {"success": True, "message": jsonpath_probe, "probe": jsonpath_probe}},
        ),
    ]

    exit_code, results = run_validations_via_pytest(
        entries=entries,
        inventory={"steps": {"setup": {"unauthorized_probe_cmd": jsonpath_probe}}},
    )

    assert exit_code == 0
    assert len(results) == 1
    assert results[0].state == State.PASSED
    assert results[0].message == jsonpath_probe


def test_run_validations_via_pytest_marks_runtime_exception() -> None:
    """A validation that raises during run() surfaces as ERROR(RUNTIME_EXCEPTION)."""
    # step_output=None makes StepSuccessCheck.run() crash on the first .get() call.
    entries = [_ready("StepSuccessCheck", "setup_checks", {"step_output": None})]

    exit_code, results = run_validations_via_pytest(entries=entries)

    assert exit_code != 0
    assert len(results) == 1
    assert results[0].state == State.ERROR
    assert results[0].error_reason == ErrorReason.RUNTIME_EXCEPTION


def test_run_validations_via_pytest_marks_unselected_entries_as_excluded() -> None:
    """Entries filtered out by pytest -k surface as SKIPPED(EXCLUDED)."""
    entries = [
        _ready("StepSuccessCheck", "setup_checks", {"step_output": {"success": True}}),
        _ready("NimHealthCheck", "nim", {"step_output": {"healthy": True}}),
    ]

    _exit_code, results = run_validations_via_pytest(entries=entries, extra_pytest_args=["-k", "StepSuccessCheck"])

    by_name = {result.entry.name: result for result in results}
    assert by_name["StepSuccessCheck"].state == State.PASSED
    nim = by_name["NimHealthCheck"]
    assert nim.state == State.SKIPPED
    assert nim.skip_reason == SkipReason.EXCLUDED
    assert nim.message == "excluded by pytest -k/-m filter"
