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

Per-entry axis placement uses these sources (union):
  1. Canonical suite files - which checks appear in each
     ``isvctl/configs/suites/*.yaml`` and that suite's declared
     ``tests.platform`` / ``tests.module`` axis key. A module-suite check's
     declared ``platforms:`` list contributes its platform placement (a
     platform-suite check's placement is its suite's platform).
  2. Wiring labels (modules only) - when a check is not wired in any suite
     file, module-axis labels on its ``labels:`` list contribute module
     placement (e.g. ``labels: [observability]`` implies the ``observability``
     module). Platform placement is never inferred from labels - it comes
     from platform-suite placement or a declared ``platforms:`` field.
"""

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml
from isvreporter.version import get_version

from isvtest.core.discovery import discover_all_tests
from isvtest.release_manifest import INCLUDE_UNRELEASED_ENV, load_released_test_filter

logger = logging.getLogger(__name__)

# Version of the catalog document envelope (schemaVersion field), bumped only
# when the top-level shape changes - independent of the isvtest package version
# (isvTestVersion), which tracks the test content.
CATALOG_SCHEMA_VERSION = 1


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


def _extract_check_test_ids_from_config(config_path: Path) -> dict[str, set[str]]:
    """Extract per-check ``test_id`` declared on a config's validation wiring.

    The ``"N/A"`` sentinel marks an intentional gap (no plan item) and is
    skipped so it never appears as a test id.
    """
    result: dict[str, set[str]] = {}
    for name, params in iter_config_checks(config_path):
        test_id = params.get("test_id")
        if isinstance(test_id, str) and test_id and test_id != "N/A":
            result.setdefault(name, set()).add(test_id)
    return result


def _build_check_attribute_map(
    extract_fn: Callable[[Path], dict[str, set[str]]],
) -> dict[str, set[str]]:
    """Map check name -> a per-check attribute unioned across all config wiring.

    Scans every config (suites AND providers, not just the canonical suites:
    on-host ``bm_*`` checks are wired only in provider configs). Names are kept
    as wired: wiring names are globally unique (enforced by
    ``scripts/validate_suite_wiring.py``), so a variant's values never smear
    onto its base class or sibling variants. Shared by ``build_label_map`` and
    ``build_test_id_map``.
    """
    configs_dir = _find_configs_dir()
    if not configs_dir:
        return {}

    attribute_map: dict[str, set[str]] = {}
    for config_path in sorted(configs_dir.rglob("*.yaml")):
        for name, values in extract_fn(config_path).items():
            attribute_map.setdefault(name, set()).update(values)
    return attribute_map


def _all_wired_names() -> set[str]:
    """Every check name as wired in any suite or provider config (raw, exact)."""
    configs_dir = _find_configs_dir()
    if not configs_dir:
        return set()
    names: set[str] = set()
    for config_path in sorted(configs_dir.rglob("*.yaml")):
        names.update(_extract_checks_from_config(config_path))
    return names


def build_test_id_map() -> dict[str, set[str]]:
    """Map check name -> test_ids declared on its suite/provider YAML wiring.

    test_ids live on the per-check YAML wiring, so every config is scanned and
    the ``test_id`` declared on each check is unioned (excluding the ``"N/A"``
    sentinel), mirroring ``build_label_map``.
    """
    return _build_check_attribute_map(_extract_check_test_ids_from_config)


def build_label_map() -> dict[str, set[str]]:
    """Map check name -> labels declared on its suite/provider YAML wiring.

    Labels live on the per-check YAML wiring, so every config is scanned and the
    ``labels:`` declared on each check is unioned. Shared by the catalog and
    ``isvctl docs`` so both report the same labels.
    """
    return _build_check_attribute_map(_extract_check_labels_from_config)


def build_label_file_map() -> dict[str, set[str]]:
    """Map label -> config files (relative to ``isvctl/configs``) that declare it.

    Unlike the catalog this is a raw config scan (not release-gated): it records
    every suite/provider YAML where a label appears on a check's wiring.
    """
    configs_dir = _find_configs_dir()
    if not configs_dir:
        return {}

    label_files: dict[str, set[str]] = {}
    for config_path in sorted(configs_dir.rglob("*.yaml")):
        rel = config_path.relative_to(configs_dir).as_posix()
        for labels in _extract_check_labels_from_config(config_path).values():
            for label in labels:
                label_files.setdefault(label, set()).add(rel)
    return label_files


def _build_axis_maps(suites_dir: Path | None = None) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Map check name -> platform/module axis labels from canonical suite wiring.

    Each suite file contributes its declared ``tests.platform`` or
    ``tests.module`` value to every check wired in that file. A module-suite
    check additionally contributes its declared ``platforms:`` list as its
    platform placement (the positive capability-compatibility declaration).
    """
    configs_dir = _find_configs_dir()
    if suites_dir is None:
        suites_dir = configs_dir / "suites" if configs_dir else None

    platform_map: dict[str, set[str]] = {}
    module_map: dict[str, set[str]] = {}
    if suites_dir is None:
        return platform_map, module_map

    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        tests = (data or {}).get("tests", {})
        if not isinstance(tests, dict):
            continue
        mod = tests.get("module")
        platform = tests.get("platform")
        if isinstance(mod, str) and mod:
            for check_name, params in iter_config_checks(path):
                module_map.setdefault(check_name, set()).add(mod)
                declared = params.get("platforms")
                if isinstance(declared, list):
                    valid = {p for p in declared if isinstance(p, str) and p}
                    if valid:
                        platform_map.setdefault(check_name, set()).update(valid)
        elif isinstance(platform, str) and platform:
            for check_name in _extract_checks_from_config(path):
                platform_map.setdefault(check_name, set()).add(platform)
    return platform_map, module_map


def _infer_module_axis_from_labels(
    name: str,
    labels: list[str],
    platform_map: dict[str, set[str]],
    module_map: dict[str, set[str]],
    module_axis: frozenset[str],
) -> None:
    """Fill the module axis map from wiring labels when a check is not in any suite file.

    Only module placement is inferred from labels; platform placement comes
    exclusively from platform-suite placement or a declared ``platforms:`` field.
    """
    if name in platform_map or name in module_map:
        return
    for label in labels:
        if label in module_axis:
            module_map.setdefault(name, set()).add(label)


def build_catalog(*, released_only: bool = True) -> list[dict[str, Any]]:
    """Discover all validation tests and return structured catalog entries.

    Each entry includes ``platforms`` and ``modules`` fields derived from
    canonical suite wiring: platform-suite placement or a declared
    ``platforms:`` field for platforms, module-suite placement (plus module
    label inference when a check is not in any suite file) for modules.
    Variant entries from configs (e.g. K8sNimHelmWorkload-1b) are included as
    separate entries inheriting metadata from their base class.

    Args:
        released_only: When True, omit tests that are not in the committed
            release manifest. Set False only when refreshing that manifest.

    Returns:
        List of catalog entry dicts, each containing:
            - name: Validation class name or variant name
            - description: Human-readable description from class metadata
            - labels: List of public label strings (e.g. ["kubernetes", "gpu"])
            - test_ids: List of test-plan ids declared on the wiring, "N/A"
              excluded (e.g. ["SEC07-01"]); empty when only intentional gaps
            - platforms: Platform-axis labels (e.g. ["kubernetes"])
            - modules: Module-axis labels (e.g. ["iam"])
    """
    platform_map, module_map = _build_axis_maps()
    label_map = build_label_map()
    test_id_map = build_test_id_map()
    _platform_axis, module_axis = build_axis_taxonomy()
    module_axis_set = frozenset(module_axis)

    wired_names = _all_wired_names()

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
        }
        _infer_module_axis_from_labels(cls.__name__, labels, platform_map, module_map, module_axis_set)

    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _entry_axes(name: str) -> tuple[list[str], list[str]]:
        return sorted(platform_map.get(name, set())), sorted(module_map.get(name, set()))

    def _has_variant_wiring(class_name: str) -> bool:
        prefix = f"{class_name}-"
        return any(name.startswith(prefix) for name in wired_names)

    # Add all discovered classes. A class wired only under variant names gets
    # no bare entry: the variants are the tests, and a bare entry would carry
    # no wiring of its own.
    for name, meta in class_meta.items():
        seen.add(name)
        if name not in wired_names and _has_variant_wiring(name):
            continue
        platforms, modules = _entry_axes(name)
        catalog.append(
            {
                "name": name,
                "description": meta["description"],
                "labels": meta["labels"],
                "test_ids": sorted(test_id_map.get(name, set())),
                "platforms": platforms,
                "modules": modules,
            }
        )

    # Add variant entries wired in any config (suites or providers). Each
    # variant carries only its own wiring's labels/test_ids - the base class's
    # other wirings must not smear their scope onto it.
    variant_names = {name for name in (set(platform_map) | set(module_map) | wired_names) if "-" in name}
    for name in sorted(variant_names):
        if name in seen:
            continue
        base = name.split("-")[0]
        if name in excluded_names or base in excluded_names:
            continue
        seen.add(name)
        meta = class_meta.get(base, {})
        variant_suffix = name[len(base) :]
        desc = meta.get("description", "")
        desc = f"{desc} ({variant_suffix.lstrip('-')})" if desc else variant_suffix.lstrip("-")
        labels = sorted(label_map.get(name, set()))
        _infer_module_axis_from_labels(name, labels, platform_map, module_map, module_axis_set)
        platforms, modules = _entry_axes(name)
        catalog.append(
            {
                "name": name,
                "description": desc,
                "labels": labels,
                "test_ids": sorted(test_id_map.get(name, set())),
                "platforms": platforms,
                "modules": modules,
            }
        )

    if released_only:
        released_tests = load_released_test_filter()
        if released_tests is None:
            logger.info("Including unreleased tests in catalog because %s is enabled", INCLUDE_UNRELEASED_ENV)
        else:

            def _is_released(name: str) -> bool:
                # The manifest lists validation classes; a variant wiring of a
                # released class is released (same code path as the runtime's
                # _is_released_validation).
                return name in released_tests or name.split("-")[0] in released_tests

            omitted_names = sorted(entry["name"] for entry in catalog if not _is_released(entry["name"]))
            catalog = [entry for entry in catalog if _is_released(entry["name"])]
            if omitted_names:
                logger.info("Omitted %d unreleased tests from catalog", len(omitted_names))
                logger.debug("Unreleased tests omitted from catalog: %s", ", ".join(omitted_names))

    logger.info("Built test catalog with %d entries", len(catalog))
    return catalog


def build_axis_taxonomy(suites_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """Return ``(platforms, modules)``: the capability and concern axis labels.

    Derived from the suite axis keys (``tests.platform`` / ``tests.module``)
    under ``suites_dir`` (default: ``isvctl/configs/suites``), so adding a suite
    extends the axes automatically. A suite that declares ``module:`` contributes
    a module label; otherwise its ``platform:`` contributes a platform
    (capability) label. This is the canonical axis scanner - isvctl's
    platform/module selection and ``scripts/validate_suite_wiring.py`` both
    build on it. Malformed suites are skipped here; the wiring validator
    reports them separately.

    Both lists are label-form (lowercase, matching each entry's ``labels``) and
    sorted, so a consumer can build the capability x module matrix directly.
    """
    if suites_dir is None:
        configs_dir = _find_configs_dir()
        suites_dir = configs_dir / "suites" if configs_dir else None
    platforms: set[str] = set()
    modules: set[str] = set()
    if suites_dir is not None:
        for path in sorted(suites_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text())
            except (OSError, yaml.YAMLError):
                continue
            tests = (data or {}).get("tests", {})
            if not isinstance(tests, dict):
                continue
            module = tests.get("module")
            if isinstance(module, str) and module:
                modules.add(module)
                continue
            platform = tests.get("platform")
            if isinstance(platform, str) and platform:
                platforms.add(platform)
    return sorted(platforms), sorted(modules)


def catalog_document(entries: list[dict[str, Any]], version: str) -> dict[str, Any]:
    """Wrap catalog ``entries`` in the versioned upload/artifact envelope.

    Adds the schema version, the isvtest package version, and the platform and
    module axis lists (see :func:`build_axis_taxonomy`). The per-entry ``labels``
    are intentionally not summarized at the top level - a consumer can derive
    the label universe from the entries when needed.
    """
    platforms, modules = build_axis_taxonomy()
    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "isvTestVersion": version,
        "platforms": platforms,
        "modules": modules,
        "entries": entries,
    }


def get_catalog_version() -> str:
    """Return the installed isvtest package version.

    Returns:
        Version string (e.g. "1.2.3") or "dev" if not installed as a package.
    """
    return get_version("isvtest")
