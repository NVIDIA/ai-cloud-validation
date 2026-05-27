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
  2. Class labels - e.g. labels=("bare_metal",) implies BARE_METAL platform

This ensures checks get a platform badge in the UI even when they aren't listed
in a YAML config (e.g. Bm* checks that only run on-host, not via SSH).
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from isvreporter.version import get_version

from isvtest.core.discovery import discover_all_tests
from isvtest.core.validation import get_validation_labels
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
    "SECURITY": ["suites/security.yaml"],
    "SLURM": ["suites/slurm.yaml"],
    "VM": ["suites/vm.yaml"],
}

# Maps class-level labels to platform strings so checks that aren't listed
# in a YAML config still get the correct platform in the catalog.
# Only platform-identifying labels are included; trait labels like "gpu",
# "ssh", "workload", and "slow" are intentionally omitted.
LABEL_TO_PLATFORM: dict[str, str] = {
    "bare_metal": "BARE_METAL",
    "iam": "IAM",
    "kubernetes": "KUBERNETES",
    "network": "NETWORK",
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


def _extract_checks_from_config(config_path: Path) -> list[str]:
    """Extract all validation check names from a config file.

    Handles list format, group-defaults format (with 'checks' key as list
    or dict), and keeps variant names (e.g. 'K8sNimHelmWorkload-3b') as-is.
    """
    try:
        data = yaml.safe_load(config_path.read_text())
    except Exception:
        return []

    validations = (data or {}).get("tests", {}).get("validations", {})
    checks: list[str] = []

    for _cat, cat_config in validations.items():
        if isinstance(cat_config, dict) and "checks" in cat_config:
            checks_val = cat_config["checks"]
            if isinstance(checks_val, dict):
                checks.extend(checks_val.keys())
            else:
                for check in checks_val:
                    if isinstance(check, dict):
                        checks.extend(check.keys())
        elif isinstance(cat_config, list):
            for check in cat_config:
                if isinstance(check, dict):
                    checks.extend(check.keys())

    return checks


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

    # Build class metadata lookup, skipping classes marked for exclusion
    class_meta: dict[str, dict[str, Any]] = {}
    excluded_names: set[str] = set()
    for cls in discover_all_tests():
        if getattr(cls, "catalog_exclude", False):
            excluded_names.add(cls.__name__)
            continue
        labels = list(get_validation_labels(cls))
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
        catalog.append(
            {
                "name": name,
                "description": desc,
                "labels": meta.get("labels", []),
                "module": meta.get("module", ""),
                "platforms": sorted(platforms),
            }
        )

    if released_only:
        released_tests = load_released_test_filter()
        if released_tests is None:
            logger.info("Including unreleased tests in catalog because %s is enabled", INCLUDE_UNRELEASED_ENV)
        else:
            before = len(catalog)
            catalog = [entry for entry in catalog if entry["name"] in released_tests]
            omitted = before - len(catalog)
            if omitted:
                logger.info("Omitted %d unreleased tests from catalog", omitted)

    logger.info("Built test catalog with %d entries", len(catalog))
    return catalog


def get_catalog_version() -> str:
    """Return the installed isvtest package version.

    Returns:
        Version string (e.g. "1.2.3") or "dev" if not installed as a package.
    """
    return get_version("isvtest")
