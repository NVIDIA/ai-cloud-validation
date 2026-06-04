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

"""Tests for validation resolution."""

import logging
from typing import Any, cast

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


class KubernetesSlowCheck(BaseValidation):
    """Validation used by parser tests (labels supplied via wiring)."""

    def run(self) -> None:
        """Mark the validation passed."""
        self.set_passed()


class AcceleratorCheck(BaseValidation):
    """Validation used by parser tests (labels supplied via wiring)."""

    def run(self) -> None:
        """Mark the validation passed."""
        self.set_passed()


class PlainCheck(BaseValidation):
    """Validation without labels used by parser tests."""

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
    labels: tuple[str, ...] = (),
) -> ValidationEntry:
    """Build a minimal validation entry."""
    return ValidationEntry(
        name=name,
        category=category,
        params_template={} if params is None else params,
        step=step,
        phase=phase,
        labels=labels,
    )


def _resolve(
    entry: ValidationEntry,
    *,
    step_outputs: dict[str, dict[str, Any]] | None = None,
    step_phases: dict[str, str] | None = None,
    requested_phases: set[str] | None = None,
    include_labels: set[str] | None = None,
    exclude_labels: set[str] | None = None,
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
        include_labels=set() if include_labels is None else include_labels,
        exclude_labels=set() if exclude_labels is None else exclude_labels,
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
        (_entry("LabelCheck", labels=("accelerator",)), {"exclude_labels": {"accelerator"}}, SkipReason.EXCLUDED),
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
        _entry("SlowCheck", labels=("slow",)),
    ]
    kwargs: dict[str, Any] = {
        "step_outputs": {},
        "step_phases": {},
        "requested_phases": {"test"},
        "include_labels": set(),
        "exclude_labels": {"slow"},
        "exclude_tests": set(),
        "released_tests": None,
        "render_context": {},
    }

    first = resolve_entries(entries, **kwargs)
    second = resolve_entries([resolved.entry for resolved in first], **kwargs)

    assert second == first


def test_resolve_entries_requires_all_include_labels() -> None:
    """Label selection is an AND filter across all requested labels."""
    entries = [
        _entry("GpuSlowCheck", labels=("gpu", "slow")),
        _entry("GpuOnlyCheck", labels=("gpu",)),
        _entry("SlowOnlyCheck", labels=("slow",)),
    ]

    results = resolve_entries(
        entries,
        step_outputs={},
        step_phases={},
        requested_phases={"test"},
        include_labels={"gpu", "slow"},
        exclude_labels=set(),
        exclude_tests=set(),
        released_tests=None,
        render_context={},
    )

    by_name = {result.entry.name: result for result in results}
    assert by_name["GpuSlowCheck"].is_ready
    assert by_name["GpuOnlyCheck"].skip_reason == SkipReason.EXCLUDED
    assert by_name["SlowOnlyCheck"].skip_reason == SkipReason.EXCLUDED
    assert "labels" in by_name["GpuOnlyCheck"].message


def test_parse_validations_supports_group_defaults_and_labels() -> None:
    """Parser expands config groups and populates labels from the wiring."""
    raw_config: dict[str, Any] = {
        "cluster": {
            "step": "create_cluster",
            "phase": "setup",
            "checks": {
                "KubernetesSlowCheck": {"expected": 4, "labels": ["slow", "kubernetes"]},
                "AcceleratorCheck": {"expected": 8, "labels": ["accelerator", "long_running"]},
                "PlainCheck": {},
            },
        },
    }

    entries = parse_validations(raw_config)

    assert entries == [
        ValidationEntry(
            name="KubernetesSlowCheck",
            category="cluster",
            params_template={"expected": 4, "labels": ["slow", "kubernetes"]},
            step="create_cluster",
            phase="setup",
            labels=("slow", "kubernetes"),
        ),
        ValidationEntry(
            name="AcceleratorCheck",
            category="cluster",
            params_template={"expected": 8, "labels": ["accelerator", "long_running"]},
            step="create_cluster",
            phase="setup",
            labels=("accelerator", "long_running"),
        ),
        ValidationEntry(
            name="PlainCheck",
            category="cluster",
            params_template={},
            step="create_cluster",
            phase="setup",
            labels=(),
        ),
    ]


def test_parse_validations_preserves_list_order() -> None:
    """Parser keeps list-format validation order for report and execution order."""
    raw_config: dict[str, Any] = {
        "checks": [
            {"PlainCheck": {"step": "first"}},
            {"PlainCheck": {"step": "second"}},
        ],
    }

    entries = parse_validations(raw_config)

    assert [entry.step for entry in entries] == ["first", "second"]


def test_parse_validations_emits_invalid_for_non_dict_list_items() -> None:
    """Stray scalars/lists in YAML produce ERROR(invalid_config) instead of vanishing."""
    raw_config: dict[str, Any] = {
        "cluster": {
            "checks": [
                {"PlainCheck": {}},
                "this-is-not-a-mapping",  # malformed
            ],
        },
        "top_level_list": [
            {"PlainCheck": {}},
            ["also-not-a-mapping"],  # malformed
        ],
    }

    entries = parse_validations(raw_config)

    invalid = [entry for entry in entries if "_invalid_config" in entry.params_template]
    assert len(invalid) == 2, "both malformed list items must surface as <invalid> entries"
    assert all(entry.name == "<invalid>" for entry in invalid)


def test_resolve_entries_treats_variant_names_as_released() -> None:
    """``ClassName-Variant`` resolves against the bare ClassName in the manifest.

    Mirrors the pytest-discovery path's ``_is_released_validation`` so the
    pre-resolution gate doesn't diverge from runtime test discovery.
    """
    entry = _entry("PlainCheck-myCustomVariant")

    resolved = _resolve(entry, released_tests={"PlainCheck"})

    assert resolved.skip_reason is None, "variant of a released class must not be marked UNRELEASED"


def test_resolve_entries_warns_when_default_filter_masks_missing_step_field(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd step-output field surfaces a WARNING even when default(...) catches it.

    Regression coverage for the silent-default-fallback bug from PR #191:
    without this, a missing field reads as a passing test instead of a
    visible mistake. The warning surfaces the failing reference name.
    """
    # ``isvtest`` logger has propagate=False (see core/logger.py), so caplog's
    # root handler doesn't see resolver warnings. Re-enable propagation here.
    monkeypatch.setattr(logging.getLogger("isvtest"), "propagate", True)

    entry = _entry(
        params={"count": "{{ steps.setup.kubernetes.node_count_invalid | default(1, true) }}"},
        step="setup",
    )
    step_output = {"kubernetes": {"node_count": 4}}

    with caplog.at_level("WARNING", logger="isvtest.core.resolution"):
        _resolve(
            entry,
            step_outputs={"setup": step_output},
            step_phases={"setup": "setup"},
            requested_phases={"setup", "test"},
            render_context={"steps": {"setup": step_output}},
        )

    assert "default(" in caplog.text, "default(...) wrapper should log a warning when masking Undefined"
    assert "node_count_invalid" in caplog.text, "warning must surface the missing field name"
