# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider-scoped label discovery helpers."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from isvtest.core.resolution import ValidationEntry, parse_validations

from isvctl.config.merger import merge_yaml_files
from isvctl.config.suite_resolution import EXTERNAL_PROVIDERS_DIRNAME, provider_dir


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
    """Return provider names, in-tree or fetched, that expose a discoverable ``config/*.yaml`` directory."""
    names = set()
    for providers_dir in (configs_root / "providers", configs_root / EXTERNAL_PROVIDERS_DIRNAME):
        if providers_dir.is_dir():
            names.update(
                path.name
                for path in providers_dir.iterdir()
                if path.is_dir() and any((provider_dir(path.name, configs_root) / "config").glob("*.yaml"))
            )
    return sorted(names)


def available_labels(provider: str, *, configs_root: Path) -> set[str]:
    """Return every label declared across a provider's resolved config wiring."""
    provider_config_dir = provider_dir(provider, configs_root) / "config"
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
) -> list[ProviderConfigMatch]:
    """Return provider configs whose resolved validation wiring matches all labels."""
    requested = {label for label in labels if label}
    provider_config_dir = provider_dir(provider, configs_root) / "config"
    matches: list[ProviderConfigMatch] = []

    for config_path in sorted(provider_config_dir.glob("*.yaml")):
        matched_checks = tuple(
            MatchedCheck(category=entry.category, name=entry.name, labels=entry.labels)
            for entry in _iter_config_validations(config_path)
            if requested.issubset(entry.labels)
        )
        if matched_checks:
            matches.append(ProviderConfigMatch(config_path=config_path, matched_checks=matched_checks))
    return matches
