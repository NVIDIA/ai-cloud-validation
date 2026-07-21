# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Platform/module resolution for ``--platform`` and ``--module`` selection.

Sibling of :mod:`isvctl.config.label_discovery`. Classifies a provider's configs
by their effective ``tests`` axis key (inherited through ``import:``): a config
that declares ``module:`` is an operational concern; otherwise its ``platform:``
marks a platform suite (service line). Plans which configs to run:

* ``--platform <platform>`` runs the whole matrix column: the one config whose
  ``platform`` is ``<platform>`` (and which declares no ``module``) first, then
  every ``module:`` config with at least one column-eligible check (the rest
  are omitted from the plan). Each run carries the column platform so module
  checks declaring a ``platforms:`` restriction that excludes it are skipped.
  A column whose platform suite wires no validations (e.g. ``foundational``)
  has no platform run: the column is modules-only and admits only modules
  whose checks positively declare it.
* ``--module <mod>`` runs a single config declaring ``module: <mod>``.
* Both together intersect. Under a modules-only column the requested module
  configs run standalone with column filtering (the synthetic column has no
  platform config). Under a real column the requested modules need the
  platform session, so the plan is the column plan filtered to the requested
  modules: platform bracket plus those modules' session runs.

Module runs under a real column are **session runs**: they execute inside the
platform run's setup/teardown bracket via a ``commands[<module>@<column>]``
lifecycle, consuming the platform's step outputs at ``{{ session.* }}``. A
module whose merged config lacks that block is omitted from the column.

Classification is by the axis key, never by filename: an ``aws/config/eks.yaml``
that imports ``k8s.yaml`` inherits ``platform: kubernetes`` and is a platform suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from isvreporter.platform import PLATFORM_ALIASES as _CANONICAL_ALIASES
from isvtest.core.resolution import parse_validations

from isvctl.config.label_discovery import provider_config_dir
from isvctl.config.merger import merge_yaml_files

AxisKind = Literal["platform", "module"]

# CLI aliases accepted for platform names (e.g. ``k8s`` -> ``kubernetes``),
# derived from the reporter's canonical alias table so aliases live in one place.
PLATFORM_ALIASES = {
    alias: canonical.lower() for alias, canonical in _CANONICAL_ALIASES.items() if alias != canonical.lower()
}


class PlatformResolutionError(Exception):
    """Raised when a platform/module selection cannot be resolved."""


@dataclass(frozen=True)
class ClassifiedConfig:
    """A provider config classified by its effective kind and platform."""

    config_path: Path
    kind: AxisKind
    platform: str


@dataclass(frozen=True)
class PlannedRun:
    """A single config to run as part of a ``--platform``/``--module`` selection."""

    config_path: Path
    role: AxisKind
    platform: str
    # The platform column this run executes under (the --platform value). Set for
    # every run in a column plan - including module runs, whose own ``platform``
    # is their module name - so result upload can report the capability the
    # module was exercised under and so checks declaring a ``platforms:``
    # restriction are filtered against it. None for standalone --module runs
    # (no column, hence no platform filtering).
    column_platform: str | None = None
    # True for a module run that executes INSIDE the column's platform session
    # (after the platform's own phases, before its teardown), with the platform
    # run's step outputs mounted read-only at ``{{ session.* }}``. Only real
    # columns have sessions; modules under a modules-only column (e.g.
    # ``foundational``) run standalone.
    session: bool = False
    # The ``commands[...]`` key a session run executes (``<module>@<column>``,
    # e.g. ``storage@kubernetes``) instead of the module's own lifecycle block.
    # None for non-session runs (the orchestrator derives the key from the
    # config's axis value as before).
    commands_key: str | None = None


@dataclass(frozen=True)
class OmittedRun:
    """A module config left out of a column plan, with the operator-facing reason."""

    config_path: Path
    module: str
    reason: str


@dataclass(frozen=True)
class ColumnPlan:
    """The runs planned for a ``--platform`` column, plus the omitted modules."""

    runs: list[PlannedRun]
    omitted: list[OmittedRun]


def _effective_kind_and_platform(config_path: Path) -> tuple[AxisKind | None, str | None]:
    """Return the ``(kind, platform)`` a config resolves to after imports.

    A config that declares ``tests.module`` is a module (its module value is the
    platform); otherwise ``tests.platform`` marks it a platform suite. Declaring
    both is rejected, matching the schema's mutual-exclusion rule.
    """
    merged = merge_yaml_files([config_path])
    tests = merged.get("tests") or {}
    if not isinstance(tests, dict):
        return None, None
    module = tests.get("module")
    platform = tests.get("platform")
    has_module = isinstance(module, str) and bool(module)
    has_platform = isinstance(platform, str) and bool(platform)
    if has_module and has_platform:
        raise PlatformResolutionError(
            f"Config {config_path} declares both tests.platform and tests.module; declare exactly one."
        )
    if has_module:
        return "module", module
    if has_platform:
        return "platform", platform
    return None, None


def effective_axes(config_path: Path) -> tuple[str | None, str | None]:
    """Return the ``(capability, module)`` pair a config resolves to after imports.

    A platform suite targets its platform as the capability and exercises no
    module; a module suite exercises its module and targets no capability of
    its own. ``(None, None)`` when the config declares neither axis key.
    """
    kind, value = _effective_kind_and_platform(config_path)
    if kind == "platform":
        return value, None
    if kind == "module":
        return None, value
    return None, None


def classify_provider_configs(provider: str, *, configs_root: Path) -> list[ClassifiedConfig]:
    """Classify every config under a provider by its effective kind + platform.

    Raises:
        PlatformResolutionError: If a config declares no (or an invalid) kind.
    """
    config_dir = provider_config_dir(provider, configs_root)
    if not config_dir.is_dir():
        raise PlatformResolutionError(f"Provider {provider!r} has no config directory at {config_dir}.")
    classified: list[ClassifiedConfig] = []
    for config_path in sorted(config_dir.glob("*.yaml")):
        kind, platform = _effective_kind_and_platform(config_path)
        if kind is None or not platform:
            raise PlatformResolutionError(
                f"Config {config_path} declares neither tests.platform nor tests.module; "
                f"every provider config must inherit a platform or module suite "
                f"via import (or declare one of these keys directly)."
            )
        classified.append(ClassifiedConfig(config_path=config_path, kind=kind, platform=platform))
    return classified


def _single_config(classified: list[ClassifiedConfig], kind: AxisKind, value: str, provider: str) -> ClassifiedConfig:
    """Return the one config of ``kind`` with platform ``value``, or raise.

    Raises:
        PlatformResolutionError: When no config matches (listing the available
            values) or several do (asking for ``-f`` disambiguation).
    """
    matches = [c for c in classified if c.kind == kind and c.platform == value]
    if not matches:
        available = sorted({c.platform for c in classified if c.kind == kind})
        raise PlatformResolutionError(
            f"Provider {provider!r} has no {value!r} {kind} config. "
            f"Available {kind}s: {', '.join(available) or '(none)'}."
        )
    if len(matches) > 1:
        paths = ", ".join(str(c.config_path) for c in matches)
        raise PlatformResolutionError(
            f"Provider {provider!r} has multiple {value!r} {kind} configs ({paths}). Disambiguate with --config/-f."
        )
    return matches[0]


def _suite_platform_axis(configs_root: Path) -> dict[str, bool]:
    """Map each platform-axis value to whether its suite wires any validations.

    The axis is derived from the suite files' ``tests.platform`` keys under
    ``configs_root/suites`` (the same source as isvtest's
    ``build_axis_taxonomy``). A suite that wires no validations (e.g.
    ``foundational``) exists purely to put its capability on the axis.
    """
    axis: dict[str, bool] = {}
    suites_dir = configs_root / "suites"
    if not suites_dir.is_dir():
        return axis
    for suite_path in sorted(suites_dir.glob("*.yaml")):
        merged = merge_yaml_files([str(suite_path)])
        tests = merged.get("tests") or {}
        if not isinstance(tests, dict) or tests.get("module"):
            continue
        suite_platform = tests.get("platform")
        if isinstance(suite_platform, str) and suite_platform:
            axis[suite_platform] = bool(tests.get("validations"))
    return axis


def session_commands_key(module: str, column: str) -> str:
    """The ``commands[...]`` key a module's session lifecycle uses under ``column``."""
    return f"{module}@{column}"


def synthetic_platform_columns(configs_root: Path) -> set[str]:
    """The modules-only columns of the suite axis (validation-less platform suites).

    Used by no-column runs to recognize session-only checks: a module check
    whose ``platforms:`` declaration names none of these columns can execute
    only inside a real column's platform session.
    """
    return {column for column, wires in _suite_platform_axis(configs_root).items() if not wires}


def column_is_modules_only(column: str, *, configs_root: Path) -> bool:
    """True when ``column`` is a modules-only (validation-less platform suite) column.

    Such a column (e.g. ``foundational``) has no platform run and therefore no
    session: its modules run standalone. Unknown columns return False (the
    caller's plan/resolve path reports them).
    """
    normalized = PLATFORM_ALIASES.get(column, column)
    return _suite_platform_axis(configs_root).get(normalized) is False


def _has_session_commands(config_path: Path, key: str) -> bool:
    """Return whether the merged config declares a ``commands[<key>]`` block."""
    merged = merge_yaml_files([str(config_path)])
    commands = merged.get("commands")
    return isinstance(commands, dict) and key in commands


def _has_column_eligible_check(config_path: Path, column: str, *, require_declaration: bool) -> bool:
    """Return whether any check in the merged config may run under ``column``.

    Eligibility mirrors the runtime ``platforms:`` filter in
    :mod:`isvtest.core.resolution`: an undeclared check runs everywhere, a
    declared subset must contain the column. For a modules-only column (no
    platform run, e.g. ``foundational``) ``require_declaration`` flips the
    default: an undeclared check belongs to every real environment column, so
    only checks positively declaring the synthetic column run under it.
    """
    merged = merge_yaml_files([str(config_path)])
    validations = (merged.get("tests") or {}).get("validations")
    if not isinstance(validations, dict):
        return False
    for entry in parse_validations(validations):
        if column in entry.platforms:
            return True
        if not require_declaration and not entry.platforms:
            return True
    return False


def plan_platform_run(provider: str, platform: str, *, configs_root: Path) -> ColumnPlan:
    """Plan the configs to run for ``--platform <platform>``.

    The platform config runs first; each module config with at least one
    column-eligible check follows (the rest are omitted, with a reason, so a
    run never pays a module's setup/teardown to execute zero checks). Every
    run carries the column platform, so module checks whose ``platforms:``
    declaration excludes it are skipped under this column.

    A column whose platform suite wires no validations (e.g. ``foundational``)
    has no platform run: providers are not expected to ship a config for it,
    so a missing provider platform config is not an error and the column is
    modules-only.

    Raises:
        PlatformResolutionError: On unknown/duplicate platform configs.
    """
    normalized = PLATFORM_ALIASES.get(platform, platform)
    classified = classify_provider_configs(provider, configs_root=configs_root)
    # False = validation-less suite defines the column (no platform run);
    # True/None (real platform / column not on the suite axis) keeps today's
    # behavior: the provider must ship a platform config or we error.
    modules_only = _suite_platform_axis(configs_root).get(normalized) is False

    runs: list[PlannedRun] = []
    if not modules_only:
        platform_config = _single_config(classified, "platform", normalized, provider)
        runs.append(
            PlannedRun(
                config_path=platform_config.config_path,
                role="platform",
                platform=normalized,
                column_platform=normalized,
            )
        )

    omitted: list[OmittedRun] = []
    for module_config in sorted((c for c in classified if c.kind == "module"), key=lambda c: c.config_path):
        if not _has_column_eligible_check(module_config.config_path, normalized, require_declaration=modules_only):
            omitted.append(
                OmittedRun(
                    config_path=module_config.config_path,
                    module=module_config.platform,
                    reason=f"no checks compatible with column '{normalized}'",
                )
            )
            continue
        # Under a real column the module runs INSIDE the platform session and
        # needs a session lifecycle: a ``commands[<module>@<column>]`` block
        # consuming the platform run's ``{{ session.* }}`` outputs. A module
        # with eligible checks but no session commands cannot execute here.
        commands_key: str | None = None
        if not modules_only:
            commands_key = session_commands_key(module_config.platform, normalized)
            if not _has_session_commands(module_config.config_path, commands_key):
                omitted.append(
                    OmittedRun(
                        config_path=module_config.config_path,
                        module=module_config.platform,
                        reason=f"no session commands (commands['{commands_key}']) for column '{normalized}'",
                    )
                )
                continue
        runs.append(
            PlannedRun(
                config_path=module_config.config_path,
                role="module",
                platform=module_config.platform,
                column_platform=normalized,
                session=not modules_only,
                commands_key=commands_key,
            )
        )
    return ColumnPlan(runs=runs, omitted=omitted)


def resolve_module_configs(
    provider: str, modules: list[str], *, configs_root: Path, column_platform: str | None = None
) -> list[PlannedRun]:
    """Resolve the single ``kind: module`` config for each ``--module <module>``.

    Standalone module runs (``column_platform`` None) have no platform column,
    so no platform filtering applies. With ``column_platform`` (the
    ``--platform X --module m`` intersect form) each run executes under that
    column: checks whose ``platforms:`` declaration excludes it are skipped
    and result upload reports the ``(column, module)`` pair. The platform
    config itself is not part of the plan, and a module with zero eligible
    checks still runs (its checks all resolve to skips). The provider is
    classified once for the whole selection.

    Raises:
        PlatformResolutionError: On unknown/duplicate module configs, or a
            ``column_platform`` not on the suite-derived platform axis.
    """
    normalized_column: str | None = None
    if column_platform:
        normalized_column = PLATFORM_ALIASES.get(column_platform, column_platform)
        axis = _suite_platform_axis(configs_root)
        if normalized_column not in axis:
            raise PlatformResolutionError(
                f"Unknown platform {column_platform!r}. Platform axis: {', '.join(sorted(axis)) or '(none)'}."
            )
    classified = classify_provider_configs(provider, configs_root=configs_root)
    return [
        PlannedRun(
            config_path=_single_config(classified, "module", module, provider).config_path,
            role="module",
            platform=module,
            column_platform=normalized_column,
        )
        for module in modules
    ]
