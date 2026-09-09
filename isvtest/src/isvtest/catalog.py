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

Suite placement and capability requirements come only from canonical
``isvctl/configs/suites/*.yaml`` wiring.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml
from isvreporter.version import build_ref, get_version

from isvtest.core.composite import CompositeCheck, is_composite
from isvtest.core.discovery import discover_all_tests
from isvtest.core.resolution import DECLARABLE_CAPABILITIES, canonical_suite_name, resolve_class_key

logger = logging.getLogger(__name__)

# Version of the catalog document envelope (schemaVersion field), bumped only
# when the top-level shape changes - independent of the isvtest package version
# (isvTestVersion), which tracks the test content.
CATALOG_SCHEMA_VERSION = 2


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
    for _, name, params in iter_checks_from_data(data):
        yield name, params


def iter_checks_from_data(data: Any) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(category, check_name, params)`` from an already-parsed document.

    Lets callers that have parsed the YAML for other reasons avoid a second
    read and parse of the same file. This is the only walker of the wiring
    shape - the catalog, the suite map, and the wiring validator all read it
    here, so a new nesting form is taught to the codebase once.
    """
    validations = (data or {}).get("tests", {}).get("validations", {})
    if not isinstance(validations, dict):
        return

    def _from_mapping(category: str, mapping: Any) -> Iterator[tuple[str, str, dict[str, Any]]]:
        if isinstance(mapping, dict):
            for name, params in mapping.items():
                yield category, name, params if isinstance(params, dict) else {}

    for category, cat_config in validations.items():
        if isinstance(cat_config, dict) and "checks" in cat_config:
            checks_val = cat_config["checks"]
            if isinstance(checks_val, dict):
                yield from _from_mapping(category, checks_val)
            elif isinstance(checks_val, list):
                for check in checks_val:
                    yield from _from_mapping(category, check)
        elif isinstance(cat_config, dict):
            yield from _from_mapping(category, cat_config)
        elif isinstance(cat_config, list):
            for check in cat_config:
                yield from _from_mapping(category, check)


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
    on-host ``bm_*`` checks are wired only in provider configs), unions the
    values ``extract_fn`` pulls from each, then propagates a variant's values up
    to its base name (``Foo-bar`` -> ``Foo``) so the base entry is not left bare.
    Shared by ``build_label_map`` and ``build_test_id_map``.
    """
    configs_dir = _find_configs_dir()
    if not configs_dir:
        return {}

    attribute_map: dict[str, set[str]] = {}
    for config_path in sorted(configs_dir.rglob("*.yaml")):
        for name, values in extract_fn(config_path).items():
            attribute_map.setdefault(name, set()).update(values)

    for name, values in list(attribute_map.items()):
        base = name.split("-")[0]
        if base != name:
            attribute_map.setdefault(base, set()).update(values)
    return attribute_map


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

    This raw config scan records every suite/provider YAML where a label appears
    on a check's wiring.
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


def _iter_suite_docs() -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(path, document)`` for each canonical suite YAML, parsed once."""
    configs_dir = _find_configs_dir()
    if not configs_dir:
        logger.warning("Could not locate isvctl/configs/ directory")
        return
    for config_path in sorted((configs_dir / "suites").glob("*.yaml")):
        yield config_path, yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def _declared_capability(data: dict[str, Any]) -> str | None:
    """Return the capability a suite document declares, or None for a plain suite.

    Reads ``tests.capability`` and travels to the service as the catalog
    entry's ``capability`` field.
    """
    tests = data.get("tests") or {}
    capability = tests.get("capability") if isinstance(tests, dict) else None
    return capability if isinstance(capability, str) and capability else None


def _build_suite_map() -> dict[str, dict[str, Any]]:
    """Map suite wiring names to suite placement and requirements.

    Composites carry no validation class, so their ``description`` is read off
    the wiring here and used as the catalog description.

    Wiring names are the catalog's keys, so a duplicate would silently drop a
    test the suite proves. Reject it here rather than let last-wins hide it.
    """
    suite_map: dict[str, dict[str, Any]] = {}
    for config_path, data in _iter_suite_docs():
        capability = _declared_capability(data)
        suite = capability if capability else canonical_suite_name(config_path.stem)
        for _, check_name, params in iter_checks_from_data(data):
            if check_name in suite_map:
                raise ValueError(f"Suite wiring name {check_name!r} is not globally unique")
            requires = params.get("requires", [])
            suite_map[check_name] = {
                "suite": suite,
                "capability": capability,
                "requires": list(requires) if isinstance(requires, list) else [],
                "composite": is_composite(params),
                "description": params.get("description") or "",
            }
    return suite_map


def build_catalog() -> list[dict[str, Any]]:
    """Discover all validation tests and return structured catalog entries.

    Each entry is one suite wiring name. Plain suites carry ``requires`` while
    platform suites carry their ``capability`` key.

    Returns:
        List of catalog entry dicts, each containing:
            - name: Validation class name, variant name, or composite name
            - description: Human-readable description from class metadata, or
              from the wiring for a composite
            - labels: List of public label strings (e.g. ["kubernetes", "gpu"])
            - test_ids: List of test-plan ids declared on the wiring, "N/A"
              excluded (e.g. ["SEC07-01"]); empty when only intentional gaps
            - source: Fully qualified Python source module
            - suite: Logical suite name
            - capability: Declared capability for platform suites, otherwise null
            - requires: Capability prerequisites for plain suites
    """
    suite_map = _build_suite_map()
    label_map = build_label_map()
    test_id_map = build_test_id_map()

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
            "source": cls.__module__,
        }

    catalog: list[dict[str, Any]] = []

    for name, placement in suite_map.items():
        if placement["composite"]:
            desc = placement["description"]
            source = CompositeCheck.__module__
            class_labels: set[str] = set()
        else:
            base = resolve_class_key(name, class_meta)
            if base is None:
                logger.warning("Omitting suite wiring %s because no validation class resolves it", name)
                continue
            if name in excluded_names or base in excluded_names:
                continue
            meta = class_meta[base]
            variant_suffix = name[len(base) :] if base != name else ""
            desc = meta.get("description", "")
            if variant_suffix:
                desc = f"{desc} ({variant_suffix.lstrip('-')})" if desc else variant_suffix.lstrip("-")
            source = meta.get("source", "")
            class_labels = set(meta.get("labels", []))
        catalog.append(
            {
                "name": name,
                "description": desc,
                "labels": sorted(class_labels | label_map.get(name, set())),
                "test_ids": sorted(test_id_map.get(name, set())),
                "source": source,
                "suite": placement["suite"],
                "capability": placement["capability"],
                "requires": placement["requires"],
            }
        )

    logger.info("Built test catalog with %d entries", len(catalog))
    return catalog


def suite_vocabularies() -> tuple[list[str], list[str]]:
    """Return ``(capabilities, plain_suites)`` from one pass over the suite YAML.

    A suite file is either a platform suite (it declares a declarable
    capability) or a plain suite named after its file - one classification, so
    the two vocabularies are read off a single parse of the directory rather
    than a scan each.
    """
    capabilities: set[str] = set()
    suites: set[str] = set()
    for config_path, data in _iter_suite_docs():
        capability = _declared_capability(data)
        if capability in DECLARABLE_CAPABILITIES:
            capabilities.add(str(capability))
        else:
            suites.add(canonical_suite_name(config_path.stem))
    return sorted(capabilities), sorted(suites)


def build_capability_vocabulary() -> list[str]:
    """Return declarable capabilities derived from platform suite YAML."""
    return suite_vocabularies()[0]


def build_suite_vocabulary() -> list[str]:
    """Return plain suite names declared by canonical suite YAML."""
    return suite_vocabularies()[1]


def _assert_disjoint_vocabulary(capabilities: list[str], suites: list[str]) -> None:
    """Reject a plain suite named after a declarable capability.

    Capabilities and plain suites share one uppercased namespace downstream: the
    frontend merges ``capabilities`` and ``suites`` into a single selectable
    "test target" list, and the backend re-splits a flat selection by
    intersecting it with the declared ``capabilities``. Both are unambiguous only
    while the two namespaces are disjoint, so enforce it at the upload
    chokepoint (checked against the full reserved set, not just declared
    capabilities, so an undeclared capability word like ``slurm`` is caught too).
    """
    collisions = sorted(set(suites) & (set(capabilities) | DECLARABLE_CAPABILITIES))
    if collisions:
        raise ValueError(
            "plain suite names collide with declarable capabilities: "
            f"{', '.join(collisions)}; rename the suite file(s) so the capability "
            "and suite namespaces stay disjoint"
        )


def catalog_document(
    entries: list[dict[str, Any]],
    version: str,
    *,
    isv_test_build_ref: str | None = None,
) -> dict[str, Any]:
    """Wrap catalog ``entries`` in the versioned upload/artifact envelope.

    Adds the schema version, the isvtest package version, and the catalog axis
    vocabulary expected by the backend upload contract. The per-entry
    ``labels`` are intentionally not summarized at the top level - a consumer
    can derive the label universe from the entries when needed.

    The build reference and digest travel in the artifact so later reporting or
    publication steps describe the code that generated it, not the machine that
    happens to read it.
    """
    capabilities, suites = suite_vocabularies()
    _assert_disjoint_vocabulary(capabilities, suites)
    reference = build_ref() if isv_test_build_ref is None else isv_test_build_ref
    document = {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "isvTestVersion": version,
        "isvTestBuildRef": reference,
        "capabilities": capabilities,
        "suites": suites,
        "entries": entries,
    }
    document["catalogDigest"] = catalog_digest(document)
    return document


def get_catalog_version() -> str:
    """Return the installed isvtest package version.

    Returns:
        Version string (e.g. "1.2.3") or "dev" if not installed as a package.
    """
    return get_version("isvtest")


def catalog_digest(document: dict[str, Any]) -> str:
    """Return the SHA-256 identity of the complete public catalog contract.

    Version and source provenance are intentionally excluded. Set-like lists
    and entries are canonicalized before compact, key-sorted JSON encoding so
    semantically identical documents digest identically regardless of discovery
    or YAML ordering.
    """
    canonical_entries = []
    for entry in document.get("entries", []):
        canonical_entries.append(
            {
                "name": entry.get("name", ""),
                "description": entry.get("description", ""),
                "labels": sorted(set(entry.get("labels") or [])),
                "capability": entry.get("capability"),
                "suite": entry.get("suite", ""),
                "requires": sorted(set(entry.get("requires") or [])),
                "test_ids": sorted(set(entry.get("test_ids") or [])),
            }
        )
    canonical_entries.sort(key=lambda entry: json.dumps(entry, sort_keys=True, separators=(",", ":")))
    contract = {
        "schemaVersion": document.get("schemaVersion", CATALOG_SCHEMA_VERSION),
        "capabilities": sorted(set(document.get("capabilities") or [])),
        "suites": sorted(set(document.get("suites") or [])),
        "entries": canonical_entries,
    }
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
