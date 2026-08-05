# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve one platform or plain suite to a canonical or provider configuration."""

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from isvtest.core.resolution import canonical_suite_name as _normalize_name

from isvctl.config.merger import merge_yaml_files

# The canonical suite/provider config tree shipped with isvctl. Lives here
# rather than on a CLI command module so every entry point (test, deploy, ...)
# sources it from the config layer that consumes it.
CONFIGS_ROOT = Path(__file__).resolve().parents[3] / "configs"


class SuiteResolutionError(Exception):
    """Raised when a suite selection cannot be resolved unambiguously."""


@dataclass(frozen=True)
class ResolvedSuite:
    """A configuration selected for one logical suite."""

    config_path: Path
    name: str
    platform: str | None


@cache
def platform_vocabulary(configs_root: Path) -> frozenset[str]:
    """Return declarable capabilities from canonical platform suite YAML.

    Cached: the suite directory is fixed for the life of a CLI invocation, and
    several entry points ask for the vocabulary two or three times per run.
    """
    platforms: set[str] = set()
    for path in (configs_root / "suites").rglob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        platform = (data.get("tests") or {}).get("capability") if isinstance(data, dict) else None
        if isinstance(platform, str) and platform:
            platforms.add(_normalize_name(platform))
    return frozenset(platforms)


@cache
def suite_vocabulary(configs_root: Path) -> frozenset[str]:
    """Return plain suite names declared by canonical suite YAML."""
    declarable = platform_vocabulary(configs_root)
    names = {_normalize_name(path.stem) for path in (configs_root / "suites").rglob("*.yaml")}
    return frozenset(names - declarable)


def parse_capability(value: str | None, configs_root: Path) -> str | None:
    """Parse the single capability context (one platform suite name).

    The four capabilities are mutually exclusive execution environments (you run
    on kubernetes OR slurm OR vm OR bare_metal, never a combination), so this
    takes exactly one value.
    """
    if value is None:
        return None
    if "," in value:
        raise SuiteResolutionError(
            f"--capability takes a single platform (got {value!r}); the capabilities "
            "are mutually exclusive execution environments, so only one runs at a time."
        )
    capability = _normalize_name(value)
    allowed = platform_vocabulary(configs_root)
    if capability not in allowed:
        raise SuiteResolutionError(
            f"Unknown or non-declarable capability: {value}. "
            f"Available capabilities: {', '.join(sorted(allowed)) or '(none)'}."
        )
    return capability


def _raw_imports(config_path: Path) -> list[str]:
    """Return import paths declared directly by a provider config."""
    try:
        data: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SuiteResolutionError(f"Failed to read {config_path}: {exc}") from exc
    value = data.get("import") if isinstance(data, dict) else None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


@cache
def _suite_name(config_path: Path, declarable: frozenset[str]) -> tuple[str, str | None]:
    """Return the logical suite name and optional platform key for a config.

    Cached: classifying a config merges it with the suite it imports, and a
    single ``isvctl test run`` asks for the same config's identity from both
    ``resolve_suite`` and ``resolve_suite_name``.
    """
    merged = merge_yaml_files([config_path])
    tests = merged.get("tests") or {}
    platform = tests.get("capability") if isinstance(tests, dict) else None
    if isinstance(platform, str) and _normalize_name(platform) in declarable:
        normalized = _normalize_name(platform)
        return normalized, normalized

    suite_imports = [Path(value).stem for value in _raw_imports(config_path) if "suites" in Path(value).parts]
    if len(suite_imports) > 1:
        raise SuiteResolutionError(
            f"Config {config_path} imports multiple suites ({', '.join(suite_imports)}); use --config/-f."
        )
    name = suite_imports[0] if suite_imports else config_path.stem
    return _normalize_name(name), None


def resolve_suite_name(config_paths: list[Path], configs_root: Path) -> str | None:
    """Return the suite name a set of ``-f`` configs resolves to.

    A run's identity is (suite, capability), so the suite has to be recoverable
    from every entry path -- including ``-f lab.yaml -f commands.yaml
    -f suites/k8s.yaml``, where the first config is not the suite. Classify each
    config in order and take the first that names a known platform or plain
    suite; fall back to the first config's stem when nothing matches, which is
    the best available label for an ad-hoc config.
    """
    if not config_paths:
        return None

    declarable = platform_vocabulary(configs_root)
    known = declarable | suite_vocabulary(configs_root)
    for path in config_paths:
        try:
            name, _ = _suite_name(path, declarable)
        except SuiteResolutionError:
            continue
        if name in known:
            return name
    return _normalize_name(config_paths[0].stem)


def resolve_suite(provider: str | None, suite: str, *, configs_root: Path) -> ResolvedSuite:
    """Resolve one canonical suite, or one provider config when a provider is selected."""
    if provider is None:
        config_dir = configs_root / "suites"
        source = "Canonical suite catalog"
    else:
        config_dir = configs_root / "providers" / provider / "config"
        source = f"Provider {provider!r}"
        if not config_dir.is_dir():
            raise SuiteResolutionError(f"Provider {provider!r} has no config directory at {config_dir}.")

    requested = _normalize_name(suite)
    declarable = platform_vocabulary(configs_root)
    # Best-effort per file: one malformed config must not fail `--suite` for the
    # whole provider. Matches `resolve_suite_name`, which already skips them.
    classified = []
    pattern = "*.yaml" if provider is not None else "**/*.yaml"
    for path in sorted(config_dir.glob(pattern)):
        try:
            classified.append((path, *_suite_name(path, declarable)))
        except SuiteResolutionError:
            continue
    matches = [item for item in classified if item[1] == requested]
    if not matches:
        available = sorted({name for _, name, _ in classified})
        raise SuiteResolutionError(
            f"{source} has no {requested!r} suite. Available suites: {', '.join(available) or '(none)'}."
        )
    if len(matches) > 1:
        paths = ", ".join(str(path) for path, _, _ in matches)
        raise SuiteResolutionError(
            f"{source} has multiple {requested!r} suite configs ({paths}). Disambiguate with --config/-f."
        )
    path, name, platform = matches[0]
    return ResolvedSuite(config_path=path, name=name, platform=platform)


def select_suite(
    suite: str,
    config_files: list[Path],
    provider: str | None,
    *,
    configs_root: Path,
) -> tuple[ResolvedSuite, str]:
    """Resolve ``--suite`` for a CLI command, with the line announcing the choice.

    ``test run`` and ``deploy run`` both offer ``--suite``; the rule it excludes
    and the wording of the announcement belong to the selection, not to either
    command, so neither can drift from the other.
    """
    if config_files:
        raise SuiteResolutionError("--suite cannot be combined with --config/-f.")
    selected = resolve_suite(provider, suite, configs_root=configs_root)
    if provider:
        return selected, f"Selected {selected.name!r} suite for provider {provider!r}."
    return selected, f"Selected canonical {selected.name!r} suite."
