# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for validation resolution."""

from typing import Any, ClassVar, cast

import pytest

from isvtest.core.resolution import (
    ErrorReason,
    ResolvedEntry,
    SkipReason,
    State,
    ValidationEntry,
    parse_validations,
    resolve_entries,
)
from isvtest.core.validation import BaseValidation


class MarkerCheck(BaseValidation):
    """Validation with markers used by parser tests."""

    markers: ClassVar[list[str]] = ["slow", "kubernetes"]

    def run(self) -> None:
        """Mark the validation passed."""
        self.set_passed()


class PlainCheck(BaseValidation):
    """Validation without markers used by parser tests."""

    def run(self) -> None:
        """Mark the validation passed."""
        self.set_passed()


def _entry(
    name: str = "PlainCheck",
    *,
    category: str = "cluster",
    params: dict[str, Any] | None = None,
    step: str | None = None,
    phase: str | None = None,
    markers: tuple[str, ...] = (),
) -> ValidationEntry:
    """Build a minimal validation entry."""
    return ValidationEntry(
        name=name,
        category=category,
        params_template={} if params is None else params,
        step=step,
        phase=phase,
        markers=markers,
    )


def _resolve(
    entry: ValidationEntry,
    *,
    step_outputs: dict[str, dict[str, Any]] | None = None,
    step_phases: dict[str, str] | None = None,
    requested_phases: set[str] | None = None,
    exclude_markers: set[str] | None = None,
    exclude_tests: set[str] | None = None,
    released_tests: set[str] | None = None,
    render_context: dict[str, Any] | None = None,
) -> ResolvedEntry:
    """Resolve one entry and return the single result."""
    results = resolve_entries(
        [entry],
        step_outputs={} if step_outputs is None else step_outputs,
        step_phases={} if step_phases is None else step_phases,
        requested_phases={"test"} if requested_phases is None else requested_phases,
        exclude_markers=set() if exclude_markers is None else exclude_markers,
        exclude_tests=set() if exclude_tests is None else exclude_tests,
        released_tests=released_tests,
        render_context={} if render_context is None else render_context,
    )
    assert len(results) == 1
    return results[0]


@pytest.mark.parametrize(
    ("entry", "kwargs", "expected_reason"),
    [
        (_entry("NewCheck"), {"released_tests": {"PlainCheck"}}, SkipReason.UNRELEASED),
        (_entry("PlainCheck"), {"exclude_tests": {"PlainCheck"}}, SkipReason.EXCLUDED),
        (_entry("MarkerCheck", markers=("slow",)), {"exclude_markers": {"slow"}}, SkipReason.EXCLUDED),
        (_entry(step="create_cluster"), {"step_phases": {}}, SkipReason.STEP_NOT_CONFIGURED),
        (
            _entry(step="create_cluster"),
            {"step_phases": {"create_cluster": "test"}, "step_outputs": {}},
            SkipReason.STEP_NO_OUTPUT,
        ),
        (_entry(phase="teardown"), {"requested_phases": {"setup"}}, SkipReason.PHASE_NOT_REQUESTED),
    ],
)
def test_resolve_entries_returns_typed_skip_reasons(
    entry: ValidationEntry,
    kwargs: dict[str, Any],
    expected_reason: SkipReason,
) -> None:
    """Each decisive skip path returns a terminal skipped entry with a reason."""
    resolved = _resolve(entry, **kwargs)

    assert resolved.state == State.SKIPPED
    assert resolved.skip_reason == expected_reason
    assert resolved.error_reason is None
    assert not resolved.is_ready
    assert resolved.message


@pytest.mark.parametrize(
    ("entry", "expected_reason"),
    [
        (
            _entry(params={"expected": "{{ missing.value }}"}),
            ErrorReason.TEMPLATE_RENDER_FAILED,
        ),
        (
            ValidationEntry(
                name="PlainCheck",
                category="cluster",
                params_template=cast(dict[str, Any], ["not", "a", "dict"]),
            ),
            ErrorReason.INVALID_CONFIG,
        ),
    ],
)
def test_resolve_entries_returns_typed_error_reasons(
    entry: ValidationEntry,
    expected_reason: ErrorReason,
) -> None:
    """Template and config failures are terminal errors, not dropped entries."""
    resolved = _resolve(entry)

    assert resolved.state == State.ERROR
    assert resolved.error_reason == expected_reason
    assert resolved.skip_reason is None
    assert not resolved.is_ready
    assert resolved.message


def test_resolve_entries_renders_ready_params_and_adds_step_output() -> None:
    """A ready entry carries rendered params and the referenced step output."""
    entry = _entry(
        params={"expected": "{{ steps.create_cluster.node_count }}"},
        step="create_cluster",
    )
    step_output = {"node_count": 4, "success": True}

    resolved = _resolve(
        entry,
        step_outputs={"create_cluster": step_output},
        step_phases={"create_cluster": "test"},
        render_context={"steps": {"create_cluster": step_output}},
    )

    assert resolved.is_ready
    assert resolved.state is None
    assert resolved.rendered_params == {
        "expected": "4",
        "step_output": step_output,
        "_category": "cluster",
    }


def test_resolve_entries_allows_default_filter_for_missing_optional_values() -> None:
    """Missing optional values can be handled intentionally with Jinja default."""
    entry = _entry(
        params={"exclude_label_selector": "{{ steps.update_test_node_pool.label_selector | default('', true) }}"},
    )

    resolved = _resolve(entry, render_context={"steps": {}})

    assert resolved.is_ready
    assert resolved.rendered_params == {
        "exclude_label_selector": "",
        "_category": "cluster",
    }


def test_resolve_entries_does_not_mutate_input_params() -> None:
    """Resolution copies params before adding step_output and category metadata."""
    params = {"expected": "{{ steps.create_cluster.node_count }}"}
    entry = _entry(params=params, step="create_cluster")
    step_output = {"node_count": 4}

    _resolve(
        entry,
        step_outputs={"create_cluster": step_output},
        step_phases={"create_cluster": "test"},
        render_context={"steps": {"create_cluster": step_output}},
    )

    assert params == {"expected": "{{ steps.create_cluster.node_count }}"}
    assert entry.params_template == params


def test_resolve_entries_is_idempotent_from_original_entries() -> None:
    """Resolved entries can be reduced to entries and resolved again deterministically."""
    entries = [
        _entry("PlainCheck", params={"value": "static"}),
        _entry("MarkerCheck", markers=("slow",)),
    ]
    kwargs: dict[str, Any] = {
        "step_outputs": {},
        "step_phases": {},
        "requested_phases": {"test"},
        "exclude_markers": {"slow"},
        "exclude_tests": set(),
        "released_tests": None,
        "render_context": {},
    }

    first = resolve_entries(entries, **kwargs)
    second = resolve_entries([resolved.entry for resolved in first], **kwargs)

    assert second == first


def test_parse_validations_supports_group_defaults_and_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parser expands config groups and populates markers from discovered classes."""
    monkeypatch.setattr(
        "isvtest.core.resolution.discover_all_tests",
        lambda: [MarkerCheck, PlainCheck],
    )
    raw_config: dict[str, Any] = {
        "cluster": {
            "step": "create_cluster",
            "phase": "setup",
            "checks": {
                "MarkerCheck": {"expected": 4},
                "PlainCheck": {},
            },
        },
    }

    entries = parse_validations(raw_config)

    assert entries == [
        ValidationEntry(
            name="MarkerCheck",
            category="cluster",
            params_template={"expected": 4},
            step="create_cluster",
            phase="setup",
            markers=("slow", "kubernetes"),
        ),
        ValidationEntry(
            name="PlainCheck",
            category="cluster",
            params_template={},
            step="create_cluster",
            phase="setup",
            markers=(),
        ),
    ]


def test_parse_validations_preserves_list_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parser keeps list-format validation order for report and execution order."""
    monkeypatch.setattr(
        "isvtest.core.resolution.discover_all_tests",
        lambda: [PlainCheck],
    )
    raw_config: dict[str, Any] = {
        "checks": [
            {"PlainCheck": {"step": "first"}},
            {"PlainCheck": {"step": "second"}},
        ],
    }

    entries = parse_validations(raw_config)

    assert [entry.step for entry in entries] == ["first", "second"]
