# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Capability/module resolution for ``--capability`` and ``--module`` selection.

Sibling of :mod:`isvctl.config.label_discovery`. Classifies a provider's configs
by their effective ``tests.capability`` / ``tests.module`` key (inherited through
``import:``) and plans which configs to run:

* ``--capability <cap>`` runs the whole matrix column: the one config declaring
  ``capability: <cap>`` first, then every ``module:`` config, each with
  capability-scoped exclude labels so checks tagged with a *different* capability
  are skipped.
* ``--module <mod>`` runs a single config declaring ``module: <mod>``.

Classification is by the ``capability``/``module`` key, never by filename: an
``aws/config/eks.yaml`` that imports ``k8s.yaml`` inherits ``capability:
kubernetes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from isvctl.config.merger import merge_yaml_files

CAPABILITY_KIND = "capability"
MODULE_KIND = "module"
# CLI aliases accepted for capability names, mirroring the orchestrator's
# ``k8s`` -> ``kubernetes`` normalization.
CAPABILITY_ALIASES = {"k8s": "kubernetes"}


class CapabilityResolutionError(Exception):
    """Raised when a capability/module selection cannot be resolved."""


@dataclass(frozen=True)
class ClassifiedConfig:
    """A provider config classified by its effective kind and platform."""

    config_path: Path
    kind: str
    platform: str


@dataclass(frozen=True)
class PlannedRun:
    """A single config to run as part of a ``--capability``/``--module`` selection."""

    config_path: Path
    role: str  # "capability" or "module"
    platform: str
    exclude_labels: tuple[str, ...] = ()


def _effective_kind_and_platform(config_path: Path) -> tuple[str | None, str | None]:
    """Return the ``(kind, platform)`` a config resolves to after imports.

    ``kind`` is ``"capability"`` or ``"module"`` depending on which axis key the
    config declares; ``platform`` is that key's value (the commands[...] key).
    """
    merged = merge_yaml_files([config_path])
    tests = merged.get("tests") or {}
    if not isinstance(tests, dict):
        return None, None
    capability = tests.get("capability")
    module = tests.get("module")
    if isinstance(capability, str) and capability:
        return CAPABILITY_KIND, capability
    if isinstance(module, str) and module:
        return MODULE_KIND, module
    return None, None


def capability_label_universe(configs_root: Path) -> frozenset[str]:
    """Return every capability label defined by the shipped capability suites.

    Derived from ``configs_root/suites`` (not the provider's configs) so exclusion
    is stable even when a provider is missing a capability config.
    """
    suites_dir = configs_root / "suites"
    universe: set[str] = set()
    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        tests = (data or {}).get("tests", {})
        if not isinstance(tests, dict):
            continue
        capability = tests.get("capability")
        if isinstance(capability, str) and capability:
            universe.add(capability)
    return frozenset(universe)


def _provider_config_dir(provider: str, configs_root: Path) -> Path:
    """Return a provider's config directory, erroring if it is absent."""
    provider_config_dir = configs_root / "providers" / provider / "config"
    if not provider_config_dir.is_dir():
        raise CapabilityResolutionError(f"Provider {provider!r} has no config directory at {provider_config_dir}.")
    return provider_config_dir


def classify_provider_configs(provider: str, *, configs_root: Path) -> list[ClassifiedConfig]:
    """Classify every config under a provider by its effective kind + platform.

    Raises:
        CapabilityResolutionError: If a config declares no (or an invalid) kind.
    """
    provider_config_dir = _provider_config_dir(provider, configs_root)
    classified: list[ClassifiedConfig] = []
    for config_path in sorted(provider_config_dir.glob("*.yaml")):
        kind, platform = _effective_kind_and_platform(config_path)
        if kind not in (CAPABILITY_KIND, MODULE_KIND) or not platform:
            raise CapabilityResolutionError(
                f"Config {config_path} declares neither tests.capability nor tests.module; "
                f"every provider config must inherit a capability/module suite via import "
                f"(or declare one of these keys directly)."
            )
        classified.append(ClassifiedConfig(config_path=config_path, kind=kind, platform=platform))
    return classified


def _available_capabilities(classified: list[ClassifiedConfig]) -> list[str]:
    """Return the sorted capability platforms a provider exposes."""
    return sorted({c.platform for c in classified if c.kind == CAPABILITY_KIND})


def _available_modules(classified: list[ClassifiedConfig]) -> list[str]:
    """Return the sorted module platforms a provider exposes."""
    return sorted({c.platform for c in classified if c.kind == MODULE_KIND})


def plan_capability_run(provider: str, capability: str, *, configs_root: Path) -> list[PlannedRun]:
    """Plan the configs to run for ``--capability <capability>``.

    The capability config runs first (no excludes); each module config follows
    with ``exclude_labels`` = every *other* capability label, so module checks
    tagged for a different capability are skipped under this column.

    Raises:
        CapabilityResolutionError: On unknown/duplicate capability configs.
    """
    normalized = CAPABILITY_ALIASES.get(capability, capability)
    classified = classify_provider_configs(provider, configs_root=configs_root)

    capability_configs = [c for c in classified if c.kind == CAPABILITY_KIND and c.platform == normalized]
    if not capability_configs:
        available = _available_capabilities(classified)
        raise CapabilityResolutionError(
            f"Provider {provider!r} has no {normalized!r} capability config. "
            f"Available capabilities: {', '.join(available) or '(none)'}."
        )
    if len(capability_configs) > 1:
        paths = ", ".join(str(c.config_path) for c in capability_configs)
        raise CapabilityResolutionError(
            f"Provider {provider!r} has multiple {normalized!r} capability configs ({paths}). "
            f"Disambiguate with --config/-f."
        )

    universe = capability_label_universe(configs_root)
    exclude_labels = tuple(sorted(universe - {normalized}))

    runs: list[PlannedRun] = [
        PlannedRun(
            config_path=capability_configs[0].config_path,
            role=CAPABILITY_KIND,
            platform=normalized,
        )
    ]
    for module_config in sorted((c for c in classified if c.kind == MODULE_KIND), key=lambda c: c.config_path):
        runs.append(
            PlannedRun(
                config_path=module_config.config_path,
                role=MODULE_KIND,
                platform=module_config.platform,
                exclude_labels=exclude_labels,
            )
        )
    return runs


def resolve_module_config(provider: str, module: str, *, configs_root: Path) -> PlannedRun:
    """Resolve the single ``kind: module`` config for ``--module <module>``.

    Runs standalone (no capability context), so no capability excludes apply.

    Raises:
        CapabilityResolutionError: On unknown/duplicate module configs.
    """
    classified = classify_provider_configs(provider, configs_root=configs_root)
    module_configs = [c for c in classified if c.kind == MODULE_KIND and c.platform == module]
    if not module_configs:
        available = _available_modules(classified)
        raise CapabilityResolutionError(
            f"Provider {provider!r} has no {module!r} module config. "
            f"Available modules: {', '.join(available) or '(none)'}."
        )
    if len(module_configs) > 1:
        paths = ", ".join(str(c.config_path) for c in module_configs)
        raise CapabilityResolutionError(
            f"Provider {provider!r} has multiple {module!r} module configs ({paths}). Disambiguate with --config/-f."
        )
    return PlannedRun(config_path=module_configs[0].config_path, role=MODULE_KIND, platform=module)
