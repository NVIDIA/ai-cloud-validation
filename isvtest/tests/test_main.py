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

"""Tests for isvtest.main module."""

import pytest

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


def test_run_validations_via_pytest_skips_structured_step_skips() -> None:
    """A step-level structured skip should skip all dependent validations."""
    step_output = {"success": True, "skipped": True, "skip_reason": "No VPCs found at site"}
    entries = [
        _ready("TenantInfoCheck", "vpc_info", {"step_output": step_output}),
        _ready(
            "FieldValueCheck",
            "traffic_validation",
            {
                "step_output": step_output,
                "field": "tests.network_setup.passed",
                "expected": True,
            },
        ),
    ]

    exit_code, results = run_validations_via_pytest(entries=entries)

    assert exit_code == 0
    assert [result.state for result in results] == [State.SKIPPED, State.SKIPPED]
    assert {result.message for result in results} == {"No VPCs found at site"}


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


def test_run_pytest_tests_uses_all_selected_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Label selection forwards one pytest marker expression with AND semantics."""
    captured: dict[str, list[str]] = {}

    def fake_pytest_main(args: list[str]) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(isvtest.main.pytest, "main", fake_pytest_main)

    exit_code = isvtest.main.run_pytest_tests(labels=["gpu", "slow"])

    assert exit_code == 0
    assert captured["args"][-2:] == ["-m", "gpu and slow"]


def test_run_pytest_tests_combines_platform_and_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform and label filters use one pytest marker expression."""
    captured: dict[str, list[str]] = {}

    def fake_pytest_main(args: list[str]) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(isvtest.main.pytest, "main", fake_pytest_main)

    exit_code = isvtest.main.run_pytest_tests(platform="bare_metal", labels=["gpu"])

    assert exit_code == 0
    assert captured["args"].count("-m") == 1
    assert captured["args"][-2:] == ["-m", "bare_metal and gpu"]


def test_run_pytest_tests_omits_marker_arg_when_no_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without labels or a platform filter, no -m argument is forwarded to pytest."""
    captured: dict[str, list[str]] = {}

    def fake_pytest_main(args: list[str]) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(isvtest.main.pytest, "main", fake_pytest_main)

    exit_code = isvtest.main.run_pytest_tests()

    assert exit_code == 0
    assert "-m" not in captured["args"]
