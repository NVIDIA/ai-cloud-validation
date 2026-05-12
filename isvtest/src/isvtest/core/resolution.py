# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Validation entry parsing and resolution."""

import copy
import json
import logging
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import Any

from jinja2 import ChainableUndefined, Environment, Undefined

from isvtest.config.loader import _ternary
from isvtest.core.discovery import discover_all_tests

logger = logging.getLogger(__name__)

ADAPTER_HANDLED_CATEGORIES = {"reframe"}
DEFAULT_VALIDATION_PHASE = "test"


class State(StrEnum):
    """Terminal state of a validation in the report."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class SkipReason(StrEnum):
    """Why a skipped validation did not run."""

    EXCLUDED = "test_excluded"  # explicitly excluded by user (YAML markers/tests OR CLI -k/-m)
    PHASE_NOT_REQUESTED = "phase_not_requested"  # entry's phase wasn't in the requested phase set
    RUNTIME_SKIP = "runtime_skip"  # validation called pytest.skip(...) at runtime
    STEP_NO_OUTPUT = "step_no_output"  # step ran but produced no JSON output
    STEP_NOT_CONFIGURED = "step_not_configured"  # step the entry binds to isn't in the platform's step list
    UNRELEASED = "unreleased"  # not in released_tests.json (gated until release)


class ErrorReason(StrEnum):
    """Why an error validation could not be processed or executed."""

    INVALID_CONFIG = "invalid_config"
    RUNTIME_EXCEPTION = "runtime_exception"
    TEMPLATE_RENDER_FAILED = "template_render_failed"


@dataclass(frozen=True)
class ValidationEntry:
    """A validation declared in configuration before resolution."""

    name: str
    category: str
    params_template: dict[str, Any]
    step: str | None = None
    phase: str | None = None
    markers: tuple[str, ...] = ()


@dataclass
class ResolvedEntry:
    """Lifecycle record for a single validation entry."""

    entry: ValidationEntry
    rendered_params: dict[str, Any] | None = None
    state: State | None = None
    skip_reason: SkipReason | None = None
    error_reason: ErrorReason | None = None
    message: str = ""
    duration_seconds: float = 0.0

    @property
    def is_ready(self) -> bool:
        """Return whether the entry is ready for runtime execution."""
        return self.state is None and self.skip_reason is None and self.error_reason is None


def parse_validations(raw_config: Mapping[str, Any]) -> list[ValidationEntry]:
    """Parse raw validation config into ordered validation entries.

    Args:
        raw_config: The ``tests.validations`` mapping from isvctl config.

    Returns:
        Ordered validation entries. Adapter-handled categories are ignored
        because they are not BaseValidation pytest entries.
    """
    markers_by_name = _validation_markers_by_name()
    entries: list[ValidationEntry] = []

    for category, category_config in raw_config.items():
        if category in ADAPTER_HANDLED_CATEGORIES:
            continue
        if not isinstance(category, str):
            entries.append(_invalid_entry(str(category), "invalid", "validation category must be a string"))
            continue

        for name, params, group_step, group_phase in _iter_validation_items(category, category_config):
            entry_step = group_step
            entry_phase = group_phase
            params_template = params

            if isinstance(params_template, dict):
                params_template = copy.deepcopy(params_template)
                if entry_step is None and "step" in params_template:
                    entry_step = params_template.get("step")
                if entry_phase is None and "phase" in params_template:
                    entry_phase = params_template.get("phase")
            else:
                params_template = copy.deepcopy(params_template)

            entries.append(
                ValidationEntry(
                    name=name,
                    category=category,
                    params_template=params_template,
                    step=entry_step if isinstance(entry_step, str) else None,
                    phase=entry_phase if isinstance(entry_phase, str) else None,
                    markers=markers_by_name.get(_base_validation_name(name, markers_by_name), ()),
                )
            )

    return entries


def resolve_entries(
    entries: list[ValidationEntry],
    *,
    step_outputs: Mapping[str, dict[str, Any]],
    step_phases: Mapping[str, str],
    requested_phases: AbstractSet[str],
    exclude_markers: AbstractSet[str],
    exclude_tests: AbstractSet[str],
    released_tests: AbstractSet[str] | None,
    render_context: Mapping[str, Any],
) -> list[ResolvedEntry]:
    """Resolve validation entries into ready or terminal outcomes.

    Args:
        entries: Parsed validation entries.
        step_outputs: Step outputs accumulated so far.
        step_phases: Mapping of configured, non-skipped step names to phases.
        requested_phases: Phase names requested by the invocation.
        exclude_markers: Validation markers excluded by config.
        exclude_tests: Validation names excluded by config.
        released_tests: Released test manifest, or None when unreleased checks are included.
        render_context: Jinja context for validation parameter rendering.

    Returns:
        A resolved entry for every input entry, in input order.
    """
    resolved: list[ResolvedEntry] = []
    env = _create_jinja_env()

    for entry in entries:
        config_error = _validate_entry_shape(entry)
        if config_error:
            resolved.append(_error(entry, ErrorReason.INVALID_CONFIG, config_error))
            continue

        # Variant-aware match: a configured ``ClassName-Variant`` is considered
        # released when the bare ``ClassName`` is in the manifest, mirroring the
        # pytest-discovery path (``_is_released_validation`` in test_validations).
        if released_tests is not None and resolve_class_key(entry.name, released_tests) is None:
            resolved.append(
                _skip(
                    entry,
                    SkipReason.UNRELEASED,
                    f"validation '{entry.name}' is not in released_tests.json",
                )
            )
            continue

        if entry.name in exclude_tests:
            resolved.append(_skip(entry, SkipReason.EXCLUDED, f"validation '{entry.name}' is excluded by name"))
            continue

        marker_matches = sorted(set(entry.markers).intersection(exclude_markers))
        if marker_matches:
            marker_list = ", ".join(marker_matches)
            resolved.append(
                _skip(
                    entry,
                    SkipReason.EXCLUDED,
                    f"validation '{entry.name}' is excluded by marker: {marker_list}",
                )
            )
            continue

        if entry.step and entry.step not in step_phases:
            resolved.append(
                _skip(
                    entry,
                    SkipReason.STEP_NOT_CONFIGURED,
                    f"step '{entry.step}' is not configured for this run",
                )
            )
            continue

        if entry.step and entry.step not in step_outputs:
            resolved.append(
                _skip(
                    entry,
                    SkipReason.STEP_NO_OUTPUT,
                    f"step '{entry.step}' did not produce output",
                )
            )
            continue

        validation_phase = get_entry_phase(entry, step_phases)
        if validation_phase not in requested_phases:
            resolved.append(
                _skip(
                    entry,
                    SkipReason.PHASE_NOT_REQUESTED,
                    f"phase '{validation_phase}' was not requested",
                )
            )
            continue

        try:
            rendered_params = _render_params(env, entry.params_template, render_context)
        except Exception as exc:
            resolved.append(
                _error(
                    entry,
                    ErrorReason.TEMPLATE_RENDER_FAILED,
                    f"failed to render validation parameters: {exc}",
                )
            )
            continue

        if not isinstance(rendered_params, dict):
            resolved.append(
                _error(
                    entry,
                    ErrorReason.INVALID_CONFIG,
                    f"validation '{entry.name}' parameters must render to a mapping",
                )
            )
            continue

        if entry.step:
            rendered_params.pop("step", None)
            rendered_params["step_output"] = copy.deepcopy(step_outputs[entry.step])
        rendered_params.pop("phase", None)
        rendered_params["_category"] = entry.category

        resolved.append(ResolvedEntry(entry=entry, rendered_params=rendered_params))

    return resolved


def get_entry_phase(entry: ValidationEntry, step_phases: Mapping[str, str]) -> str:
    """Return the phase a validation entry belongs to."""
    if entry.phase:
        return entry.phase
    if entry.step:
        return step_phases.get(entry.step, DEFAULT_VALIDATION_PHASE)
    return DEFAULT_VALIDATION_PHASE


def format_resolution_message(entry: ResolvedEntry) -> str:
    """Return the operator-facing message for a resolved entry."""
    if entry.message:
        return entry.message
    if entry.skip_reason:
        return entry.skip_reason.value
    if entry.error_reason:
        return entry.error_reason.value
    return ""


@cache
def _validation_markers_by_name() -> dict[str, tuple[str, ...]]:
    """Return discovered validation markers keyed by class name."""
    markers: dict[str, tuple[str, ...]] = {}
    for cls in discover_all_tests():
        cls_markers = getattr(cls, "markers", None) or []
        markers[cls.__name__] = tuple(str(marker) for marker in cls_markers)
    return markers


def resolve_class_key(name: str, keys: Iterable[str]) -> str | None:
    """Resolve a configured validation name to its discovered class key.

    Returns the input name if it matches a key directly, otherwise the
    longest key matching a ``ClassName-Variant`` prefix, or None when no
    candidate matches.
    """
    keys_tuple = tuple(keys)
    if name in keys_tuple:
        return name
    matches = [candidate for candidate in keys_tuple if name.startswith(f"{candidate}-")]
    if not matches:
        return None
    return max(matches, key=len)


def _base_validation_name(name: str, markers_by_name: Mapping[str, tuple[str, ...]]) -> str:
    """Return the discovered base class name for a configured validation name."""
    return resolve_class_key(name, markers_by_name) or name


def _iter_validation_items(category: str, category_config: Any) -> list[tuple[str, Any, Any, Any]]:
    """Return parsed ``(name, params, group_step, group_phase)`` tuples."""
    if isinstance(category_config, dict) and "checks" in category_config:
        group_step = category_config.get("step")
        group_phase = category_config.get("phase")
        checks_val = category_config.get("checks", {})
        if isinstance(checks_val, dict):
            return [(str(name), params or {}, group_step, group_phase) for name, params in checks_val.items()]
        if isinstance(checks_val, list):
            return _expand_check_list(
                checks_val,
                group_step,
                group_phase,
                f"each item in category '{category}.checks' must be a mapping",
            )
        return [
            (
                "<invalid>",
                {"_invalid_config": f"checks for category '{category}' must be a mapping or list"},
                None,
                None,
            )
        ]

    if isinstance(category_config, list):
        return _expand_check_list(
            category_config,
            None,
            None,
            f"each item in category '{category}' must be a mapping",
        )

    if isinstance(category_config, dict):
        return [(str(name), params or {}, None, None) for name, params in category_config.items()]

    return [
        ("<invalid>", {"_invalid_config": f"category '{category}' validations must be a mapping or list"}, None, None)
    ]


def _expand_check_list(
    items: list[Any],
    group_step: Any,
    group_phase: Any,
    invalid_message: str,
) -> list[tuple[str, Any, Any, Any]]:
    """Expand a list of ``[{Name: params}, ...]`` entries to parsed tuples.

    Non-dict items become ``<invalid>`` placeholders that resolve to
    ``ERROR(invalid_config)`` so malformed YAML surfaces in the report instead
    of being silently dropped.
    """
    result: list[tuple[str, Any, Any, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(("<invalid>", {"_invalid_config": invalid_message}, None, None))
            continue
        for name, params in item.items():
            result.append((str(name), params or {}, group_step, group_phase))
    return result


def _invalid_entry(name: str, category: str, message: str) -> ValidationEntry:
    """Build a validation entry that resolves to INVALID_CONFIG."""
    return ValidationEntry(name=name, category=category, params_template={"_invalid_config": message})


def _validate_entry_shape(entry: ValidationEntry) -> str | None:
    """Return an invalid-config message, or None when the entry shape is valid."""
    if not isinstance(entry.name, str) or not entry.name:
        return "validation name must be a non-empty string"
    if not isinstance(entry.category, str) or not entry.category:
        return f"validation '{entry.name}' category must be a non-empty string"
    if not isinstance(entry.params_template, dict):
        return f"validation '{entry.name}' parameters must be a mapping"
    invalid_message = entry.params_template.get("_invalid_config")
    if invalid_message:
        return str(invalid_message)
    return None


def _render_params(env: Environment, params: dict[str, Any], render_context: Mapping[str, Any]) -> dict[str, Any]:
    """Render validation parameters recursively."""
    return {key: _render_value(env, value, render_context) for key, value in params.items()}


def _render_value(env: Environment, value: Any, render_context: Mapping[str, Any]) -> Any:
    """Render a nested validation parameter value."""
    if isinstance(value, str):
        return _render_string(env, value, render_context)
    if isinstance(value, dict):
        return {key: _render_value(env, item, render_context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(env, item, render_context) for item in value]
    return value


def _render_string(env: Environment, value: str, render_context: Mapping[str, Any]) -> str:
    """Render a single string if it contains a Jinja template."""
    if "{{" not in value or "}}" not in value:
        return value
    return env.from_string(value).render(**render_context)


def _warning_default(value: Any, default_value: Any = "", boolean: bool = False) -> Any:
    """Drop-in replacement for Jinja's ``default`` filter that warns when it
    catches an Undefined value. Without this, a typo like
    ``{{ steps.setup.node_count_invalid | default(1) }}`` silently substitutes
    the default for the missing field instead of surfacing the mistake.
    """
    if isinstance(value, Undefined):
        if value._undefined_message:
            logger.warning(f"default(...) masked: {value._undefined_message}")
        return default_value
    if boolean and not value:
        return default_value
    return value


@cache
def _create_jinja_env() -> Environment:
    """Return the strict Jinja environment used by resolution."""
    env = Environment(undefined=ChainableStrictUndefined)
    env.filters["tojson"] = lambda value: json.dumps(value)
    env.filters["ternary"] = _ternary
    env.filters["default"] = _warning_default
    env.filters["d"] = _warning_default
    return env


class ChainableStrictUndefined(ChainableUndefined):
    """Undefined value that supports ``default`` but errors when emitted."""

    __str__ = ChainableUndefined._fail_with_undefined_error
    __iter__ = ChainableUndefined._fail_with_undefined_error
    __bool__ = ChainableUndefined._fail_with_undefined_error


def _skip(entry: ValidationEntry, reason: SkipReason, message: str) -> ResolvedEntry:
    """Build a skipped resolved entry."""
    return ResolvedEntry(entry=entry, state=State.SKIPPED, skip_reason=reason, message=message)


def _error(entry: ValidationEntry, reason: ErrorReason, message: str) -> ResolvedEntry:
    """Build an error resolved entry."""
    return ResolvedEntry(entry=entry, state=State.ERROR, error_reason=reason, message=message)
