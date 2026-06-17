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

"""Test catalog generation for coverage tracking.

Builds a structured catalog of all available validation tests by calling
discover_all_tests() and serializing each BaseValidation subclass's metadata.
The catalog is version-keyed by the installed isvtest package version.

Platform tagging uses two sources (union of both):
  1. Config files - which checks appear in each isvctl/configs/suites/*.yaml
  2. Wiring labels - e.g. a check wired with labels: [bare_metal] implies the
     BARE_METAL platform

This ensures checks get a platform badge in the UI even when they only appear
in provider configs (e.g. Bm* checks that run on-host, not via SSH).
"""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from isvreporter.version import get_version

from isvtest.core.discovery import discover_all_tests
from isvtest.release_manifest import INCLUDE_UNRELEASED_ENV, load_released_test_filter

logger = logging.getLogger(__name__)

# Configs that define the canonical test list per platform.
# Relative to the isvctl/configs/ directory.
PLATFORM_CONFIGS: dict[str, list[str]] = {
    "BARE_METAL": ["suites/bare_metal.yaml"],
    "CONTROL_PLANE": ["suites/control-plane.yaml"],
    "IAM": ["suites/iam.yaml"],
    "IMAGE_REGISTRY": ["suites/image-registry.yaml"],
    "KUBERNETES": ["suites/k8s.yaml"],
    "NETWORK": ["suites/network.yaml"],
    "OBSERVABILITY": ["suites/observability.yaml"],
    "SECURITY": ["suites/security.yaml"],
    "SLURM": ["suites/slurm.yaml"],
    "VM": ["suites/vm.yaml"],
}

# Maps wiring labels to platform strings so a check's platform can be inferred
# from its labels when it isn't otherwise tied to a platform.
# Only platform-identifying labels are included; trait labels like "gpu",
# "ssh", "workload", and "slow" are intentionally omitted.
LABEL_TO_PLATFORM: dict[str, str] = {
    "bare_metal": "BARE_METAL",
    "control_plane": "CONTROL_PLANE",
    "iam": "IAM",
    "image_registry": "IMAGE_REGISTRY",
    "kubernetes": "KUBERNETES",
    "network": "NETWORK",
    "observability": "OBSERVABILITY",
    "security": "SECURITY",
    "slurm": "SLURM",
    "vm": "VM",
}


def _find_configs_dir() -> Path | None:
    """Locate the isvctl/configs/ directory."""
    # Walk up from this file to find the workspace root
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "isvctl" / "configs"
        if candidate.is_dir():
            return candidate
    return None


def iter_config_checks(config_path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(check_name, params)`` for every check wired in a config file.

    Walks ``tests.validations`` handling the bare-list form, the group-defaults
    form (``{step, checks: {...}|[...]}``), and the dict form. Variant names
    (e.g. ``K8sNimHelmWorkload-3b``) are kept as-is; ``params`` is normalized to
    a dict (empty when a check carries no params). Shared by the catalog and the
    test-plan coverage script so the form-handling lives in one place.
    """
    try:
        data = yaml.safe_load(config_path.read_text())
    except Exception:
        return

    validations = (data or {}).get("tests", {}).get("validations", {})
    if not isinstance(validations, dict):
        return

    def _from_mapping(mapping: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        if isinstance(mapping, dict):
            for name, params in mapping.items():
                yield name, params if isinstance(params, dict) else {}

    for cat_config in validations.values():
        if isinstance(cat_config, dict) and "checks" in cat_config:
            checks_val = cat_config["checks"]
            if isinstance(checks_val, dict):
                yield from _from_mapping(checks_val)
            elif isinstance(checks_val, list):
                for check in checks_val:
                    yield from _from_mapping(check)
        elif isinstance(cat_config, dict):
            yield from _from_mapping(cat_config)
        elif isinstance(cat_config, list):
            for check in cat_config:
                yield from _from_mapping(check)


def _extract_checks_from_config(config_path: Path) -> list[str]:
    """Extract all validation check names from a config file."""
    return [name for name, _ in iter_config_checks(config_path)]


def _extract_check_labels_from_config(config_path: Path) -> dict[str, set[str]]:
    """Extract per-check ``labels`` declared on a config's validation wiring."""
    result: dict[str, set[str]] = {}
    for name, params in iter_config_checks(config_path):
        labels = params.get("labels")
        if isinstance(labels, str):
            labels = [labels]
        if isinstance(labels, list):
            valid = {label for label in labels if isinstance(label, str) and label}
            if valid:
                result.setdefault(name, set()).update(valid)
    return result


def build_label_map() -> dict[str, set[str]]:
    """Map check name -> labels declared on its suite/provider YAML wiring.

    Labels live on the per-check YAML wiring, so this scans every config and
    unions the ``labels:`` declared on each check. A variant's labels propagate
    up to its base name so the base entry is not left bare. Shared by the
    catalog and ``isvctl docs`` so both report the same labels.
    """
    configs_dir = _find_configs_dir()
    if not configs_dir:
        return {}

    # Scan every config (suites AND providers), not just the canonical suites:
    # on-host checks (bm_*) are wired only in provider configs, so their labels
    # live there. Per-check ``labels:`` declared anywhere in YAML are unioned.
    label_map: dict[str, set[str]] = {}
    for config_path in sorted(configs_dir.rglob("*.yaml")):
        for name, labels in _extract_check_labels_from_config(config_path).items():
            label_map.setdefault(name, set()).update(labels)

    for name, labels in list(label_map.items()):
        base = name.split("-")[0]
        if base != name:
            label_map.setdefault(base, set()).update(labels)
    return label_map


def _build_platform_map() -> dict[str, set[str]]:
    """Build a mapping from test name to set of platform strings.

    Scans the canonical config files to determine which tests belong to
    which platforms.
    """
    configs_dir = _find_configs_dir()
    if not configs_dir:
        logger.warning("Could not locate isvctl/configs/ directory")
        return {}

    test_to_platforms: dict[str, set[str]] = {}

    for platform, config_files in PLATFORM_CONFIGS.items():
        for config_file in config_files:
            config_path = configs_dir / config_file
            if not config_path.exists():
                logger.debug("Config not found: %s", config_path)
                continue

            checks = _extract_checks_from_config(config_path)
            for check_name in checks:
                if check_name not in test_to_platforms:
                    test_to_platforms[check_name] = set()
                test_to_platforms[check_name].add(platform)

    return test_to_platforms


def build_catalog(*, released_only: bool = True) -> list[dict[str, Any]]:
    """Discover all validation tests and return structured catalog entries.

    Each entry includes a 'platforms' field derived from the config files,
    indicating which platforms the test belongs to. Variant entries from
    configs (e.g. K8sNimHelmWorkload-1b) are included as separate entries
    inheriting metadata from their base class.

    Args:
        released_only: When True, omit tests that are not in the committed
            release manifest. Set False only when refreshing that manifest.

    Returns:
        List of catalog entry dicts, each containing:
            - name: Validation class name or variant name
            - description: Human-readable description from class metadata
            - labels: List of public label strings (e.g. ["kubernetes", "gpu"])
            - module: Fully qualified module path
            - platforms: List of platform strings (e.g. ["KUBERNETES"])
    """
    platform_map = _build_platform_map()
    label_map = build_label_map()

    # Build class metadata lookup, skipping classes marked for exclusion
    class_meta: dict[str, dict[str, Any]] = {}
    excluded_names: set[str] = set()
    for cls in discover_all_tests():
        if getattr(cls, "catalog_exclude", False):
            excluded_names.add(cls.__name__)
            continue
        labels = sorted(label_map.get(cls.__name__, set()))
        class_meta[cls.__name__] = {
            "description": getattr(cls, "description", "") or "",
            "labels": labels,
            "module": cls.__module__,
        }
        # Infer platforms from labels only for checks not already covered by
        # canonical configs. Some labels (for example "security") are useful
        # pytest filters but are not reliable platform ownership signals once a
        # check appears in a suite file.
        if cls.__name__ not in platform_map:
            for label in labels:
                platform = LABEL_TO_PLATFORM.get(label)
                if platform:
                    platform_map.setdefault(cls.__name__, set()).add(platform)

    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Add all discovered classes
    for name, meta in class_meta.items():
        seen.add(name)
        catalog.append(
            {
                "name": name,
                "description": meta["description"],
                "labels": meta["labels"],
                "module": meta["module"],
                "platforms": sorted(platform_map.get(name, [])),
            }
        )

    # Add variant entries from configs that aren't base classes
    for name, platforms in platform_map.items():
        if name in seen:
            continue
        base = name.split("-")[0] if "-" in name else name
        if name in excluded_names or base in excluded_names:
            continue
        seen.add(name)
        meta = class_meta.get(base, {})
        variant_suffix = name[len(base) :] if base != name else ""
        desc = meta.get("description", "")
        if variant_suffix:
            desc = f"{desc} ({variant_suffix.lstrip('-')})" if desc else variant_suffix.lstrip("-")
        labels = sorted(set(meta.get("labels", [])) | label_map.get(name, set()))
        catalog.append(
            {
                "name": name,
                "description": desc,
                "labels": labels,
                "module": meta.get("module", ""),
                "platforms": sorted(platforms),
            }
        )

    if released_only:
        released_tests = load_released_test_filter()
        if released_tests is None:
            logger.info("Including unreleased tests in catalog because %s is enabled", INCLUDE_UNRELEASED_ENV)
        else:
            omitted_names = sorted(entry["name"] for entry in catalog if entry["name"] not in released_tests)
            catalog = [entry for entry in catalog if entry["name"] in released_tests]
            if omitted_names:
                logger.info("Omitted %d unreleased tests from catalog", len(omitted_names))
                logger.debug("Unreleased tests omitted from catalog: %s", ", ".join(omitted_names))

    logger.info("Built test catalog with %d entries", len(catalog))
    return catalog


def get_catalog_version() -> str:
    """Return the installed isvtest package version.

    Returns:
        Version string (e.g. "1.2.3") or "dev" if not installed as a package.
    """
    return get_version("isvtest")
