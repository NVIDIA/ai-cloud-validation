# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Platform/module resolution for ``--platform`` and ``--module`` selection.

Sibling of :mod:`isvctl.config.label_discovery`. Classifies a provider's configs
by their effective ``tests`` axis key (inherited through ``import:``): a config
that declares ``module:`` is an operational concern; otherwise its ``platform:``
marks a platform suite (service line). Plans which configs to run:

* ``--platform <platform>`` runs the whole matrix column: the one config whose
  ``platform`` is ``<platform>`` (and which declares no ``module``) first, then
  every ``module:`` config. Each run carries the column platform so module
  checks declaring a ``platforms:`` restriction that excludes it are skipped.
* ``--module <mod>`` runs a single config declaring ``module: <mod>``.

Classification is by the axis key, never by filename: an ``aws/config/eks.yaml``
that imports ``k8s.yaml`` inherits ``platform: kubernetes`` and is a platform suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from isvreporter.platform import PLATFORM_ALIASES as _CANONICAL_ALIASES

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


def plan_platform_run(provider: str, platform: str, *, configs_root: Path) -> list[PlannedRun]:
    """Plan the configs to run for ``--platform <platform>``.

    The platform config runs first; each module config follows. Every run
    carries the column platform, so module checks whose ``platforms:``
    declaration excludes it are skipped under this column.

    Raises:
        PlatformResolutionError: On unknown/duplicate platform configs.
    """
    normalized = PLATFORM_ALIASES.get(platform, platform)
    classified = classify_provider_configs(provider, configs_root=configs_root)
    platform_config = _single_config(classified, "platform", normalized, provider)

    runs: list[PlannedRun] = [
        PlannedRun(
            config_path=platform_config.config_path,
            role="platform",
            platform=normalized,
            column_platform=normalized,
        )
    ]
    for module_config in sorted((c for c in classified if c.kind == "module"), key=lambda c: c.config_path):
        runs.append(
            PlannedRun(
                config_path=module_config.config_path,
                role="module",
                platform=module_config.platform,
                column_platform=normalized,
            )
        )
    return runs


def resolve_module_configs(provider: str, modules: list[str], *, configs_root: Path) -> list[PlannedRun]:
    """Resolve the single ``kind: module`` config for each ``--module <module>``.

    Module runs are standalone (no platform column), so no platform filtering
    applies. The provider is classified once for the whole selection.

    Raises:
        PlatformResolutionError: On unknown/duplicate module configs.
    """
    classified = classify_provider_configs(provider, configs_root=configs_root)
    return [
        PlannedRun(
            config_path=_single_config(classified, "module", module, provider).config_path,
            role="module",
            platform=module,
        )
        for module in modules
    ]
