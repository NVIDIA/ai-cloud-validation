# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider-scoped label discovery helpers."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from isvtest.core.resolution import ValidationEntry, parse_validations, resolve_class_key

from isvctl.config.merger import merge_yaml_files


def _iter_config_validations(config_path: Path) -> Iterator[ValidationEntry]:
    """Yield the validation entries of a config with its imports resolved."""
    merged = merge_yaml_files([config_path])
    raw_validations = (merged.get("tests") or {}).get("validations") or {}
    yield from parse_validations(raw_validations)


@dataclass(frozen=True)
class MatchedCheck:
    """A validation check that matched requested labels."""

    category: str
    name: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ProviderConfigMatch:
    """A provider config selected by label discovery."""

    config_path: Path
    matched_checks: tuple[MatchedCheck, ...]


def list_providers(configs_root: Path) -> list[str]:
    """Return provider names that expose a discoverable ``config/*.yaml`` directory."""
    providers_dir = configs_root / "providers"
    if not providers_dir.is_dir():
        return []
    return sorted(
        provider_dir.name
        for provider_dir in providers_dir.iterdir()
        if provider_dir.is_dir() and any((provider_dir / "config").glob("*.yaml"))
    )


def available_labels(provider: str, *, configs_root: Path) -> set[str]:
    """Return every label declared across a provider's resolved config wiring."""
    provider_config_dir = configs_root / "providers" / provider / "config"
    labels: set[str] = set()
    for config_path in provider_config_dir.glob("*.yaml"):
        for entry in _iter_config_validations(config_path):
            labels.update(entry.labels)
    return labels


def discover_provider_label_configs(
    provider: str,
    labels: list[str],
    *,
    configs_root: Path,
    released_tests: set[str] | None = None,
) -> list[ProviderConfigMatch]:
    """Return provider configs whose resolved validation wiring matches all labels.

    A check counts toward a match only if it is also runnable under the release
    filter, mirroring orchestrator execution: when ``released_tests`` is a set,
    unreleased checks are ignored so a config is not selected solely on a check
    that would be skipped at runtime. ``None`` disables the filter (include all),
    matching ``ISVTEST_INCLUDE_UNRELEASED``.
    """
    requested = {label for label in labels if label}
    provider_config_dir = configs_root / "providers" / provider / "config"
    matches: list[ProviderConfigMatch] = []

    for config_path in sorted(provider_config_dir.glob("*.yaml")):
        matched_checks = tuple(
            MatchedCheck(category=entry.category, name=entry.name, labels=entry.labels)
            for entry in _iter_config_validations(config_path)
            if requested.issubset(entry.labels)
            and (released_tests is None or resolve_class_key(entry.name, released_tests) is not None)
        )
        if matched_checks:
            matches.append(ProviderConfigMatch(config_path=config_path, matched_checks=matched_checks))
    return matches
