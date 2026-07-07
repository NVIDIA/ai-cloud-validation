#!/usr/bin/env python3
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

"""Govern the platform x module taxonomy wired in suite YAML.

Suite configs under ``isvctl/configs/suites/`` are the source of truth for
validation metadata on this branch. This validator enforces:

* ``tests.platform`` / ``tests.module`` - every suite declares exactly one of
  these axis keys. ``platform`` marks a service-line platform; ``module``
  marks an operational concern (its value is also the runtime platform). The
  platform/module *label* axes are derived from these keys (so adding a suite
  extends the axes automatically).
* ``test_id`` - a plan id from ``docs/test-plan.yaml``, or ``"N/A"`` when the
  check is generic plumbing with no plan item.
* ``labels`` - a non-empty list used for pytest selection and catalog reporting.
  Each suite check must include its declared axis label, for example checks in
  a suite with ``tests.platform: bare_metal`` must include ``bare_metal``.
* label governance - a check may carry at most one platform-axis label
  (platform-scoped exclusion is any-intersection, so two platform labels would
  skip the check under every column). Labels are otherwise free-form: they
  originate in the wiring YAML itself, so there is no external allowlist to
  validate them against.
* name uniqueness - a wiring name may appear only once across all suite files.
  The catalog, results matching, and the capability x module matrix are keyed
  by name, so a generic check class wired more than once must use a distinct
  variant name per wiring (``StepSuccessCheck-iam_teardown``); reusing a bare
  class name across wirings unions unrelated labels/test_ids onto one entry.

Provider configs under ``isvctl/configs/providers/`` are scanned for the label
governance rules only (they inherit ``kind``/``test_id`` from the suites they
import).

Usage:
    python3 scripts/validate_suite_wiring.py
    python3 scripts/validate_suite_wiring.py --check   # exit 1 on violations
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from isvtest.catalog import build_axis_taxonomy

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITES_DIR = REPO_ROOT / "isvctl" / "configs" / "suites"
_NEXT_CATEGORY_LINE = re.compile(r"^    \S")


def _check_line_patterns(check_name: str) -> tuple[re.Pattern[str], ...]:
    """Return line patterns for dict- and list-form check wiring."""
    escaped = re.escape(check_name)
    return (
        re.compile(rf"^        {escaped}:\s*$"),
        re.compile(rf"^      - {escaped}:\s*$"),
    )


def find_check_line_numbers(lines: list[str], category: str, check_name: str) -> list[int]:
    """Return 1-based line numbers where ``check_name`` is wired under ``category``."""
    category_line = re.compile(rf"^    {re.escape(category)}:\s*$")
    patterns = _check_line_patterns(check_name)
    matches: list[int] = []
    in_category = False

    for index, line in enumerate(lines):
        if category_line.match(line):
            in_category = True
            continue
        if not in_category:
            continue
        if index > 0 and _NEXT_CATEGORY_LINE.match(line) and not line.startswith("      "):
            break
        if any(pattern.match(line) for pattern in patterns):
            matches.append(index + 1)
    return matches


def _normalize_labels(value: Any) -> list[str]:
    """Return a list of non-empty label strings from YAML wiring."""
    if not isinstance(value, list):
        return []
    return [label for label in value if isinstance(label, str) and label.strip()]


def _normalize_test_id(value: Any) -> str | None:
    """Return a stripped test_id string, or None when absent/invalid."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def required_suite_label(platform: Any, module: Any) -> str | None:
    """Return the declared axis label every check in a suite must carry."""
    if isinstance(module, str) and module:
        return module
    if isinstance(platform, str) and platform:
        return platform
    return None


def _load_config(config_path: Path) -> tuple[list[str], Any]:
    """Read and parse a config file once, returning ``(lines, data)``.

    Raises:
        ValueError: When the file cannot be read or parsed.
    """
    try:
        text = config_path.read_text()
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read/parse {config_path}: {exc}") from exc
    return text.splitlines(), data


def iter_suite_checks(config_path: Path) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(category, check_name, params)`` for checks in a suite file."""
    _, data = _load_config(config_path)
    yield from _iter_checks(data)


def _iter_checks(data: Any) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(category, check_name, params)`` from parsed config data."""
    validations = (data or {}).get("tests", {}).get("validations", {})
    if not isinstance(validations, dict):
        return

    def _from_mapping(category: str, mapping: Any) -> Iterator[tuple[str, str, dict[str, Any]]]:
        """Yield wired checks from a dict- or list-form ``checks`` mapping."""
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
        elif isinstance(cat_config, list):
            for check in cat_config:
                yield from _from_mapping(category, check)


def _axis_keys(data: Any) -> tuple[Any, Any]:
    """Return the ``(platform, module)`` axis keys declared in a config's ``tests:`` block."""
    tests = (data or {}).get("tests", {})
    if not isinstance(tests, dict):
        return None, None
    return tests.get("platform"), tests.get("module")


def derive_axis_labels(suites_dir: Path = SUITES_DIR) -> tuple[frozenset[str], frozenset[str]]:
    """Derive the (platform, module) label axes from the suites' axis keys.

    Thin wrapper over the canonical scanner :func:`isvtest.catalog.build_axis_taxonomy`.
    Malformed suites are skipped there; :func:`wiring_errors` reports them separately.
    """
    platforms, modules = build_axis_taxonomy(suites_dir)
    return frozenset(platforms), frozenset(modules)


def _iter_provider_configs(providers_dir: Path) -> Iterator[Path]:
    """Yield provider config YAML files (``providers/*/config/*.yaml`` + ``providers/*.yaml``)."""
    if not providers_dir.is_dir():
        return
    yield from sorted(providers_dir.glob("*/config/*.yaml"))
    yield from sorted(providers_dir.glob("*.yaml"))


def _multiple_platform_labels_error(
    location: str,
    labels: list[str],
    platform_labels: frozenset[str],
) -> str | None:
    """Return the multiple-platform-label error for one check, or None."""
    platform_hits = sorted({label for label in labels if label in platform_labels})
    if len(platform_hits) > 1:
        return f"{location}: multiple platform labels ({', '.join(platform_hits)}); at most one is allowed"
    return None


def _format_location(config_path: Path, category: str, check_name: str, line_number: int | None) -> str:
    """Return a stable location string for error messages."""
    try:
        rel_path = config_path.relative_to(REPO_ROOT)
    except ValueError:
        rel_path = config_path
    if line_number is None:
        return f"{rel_path} → {category} → {check_name}"
    return f"{rel_path}:{line_number} → {category} → {check_name}"


def _relative(path: Path) -> Path | str:
    """Return a repo-relative path for messages, or the path when outside the repo."""
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        return path


def wiring_errors(suites_dir: Path = SUITES_DIR, providers_dir: Path | None = None) -> list[str]:
    """Return human-readable errors for incomplete/ungoverned suite check wiring.

    Validates suite files under ``suites_dir`` (kind, test_id, labels, suite
    label, and label governance) and then applies the label governance rules to
    provider configs under ``providers_dir`` (defaults to the ``providers``
    directory beside ``suites_dir``), which inherit kind/test_id via ``import:``.
    The platform/module label axes are derived from ``suites_dir``.
    """
    if providers_dir is None:
        providers_dir = suites_dir.parent / "providers"

    platform_labels, _module_labels = derive_axis_labels(suites_dir)

    errors: list[str] = []
    # wiring name -> suite files that wire it (names must be globally unique)
    name_files: dict[str, list[str]] = defaultdict(list)

    def _located_checks(path: Path, lines: list[str], data: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield ``(location, params)`` for each wired check, with line attribution."""
        occurrence: dict[tuple[str, str], int] = defaultdict(int)
        for category, name, params in _iter_checks(data):
            key = (category, name)
            line_numbers = find_check_line_numbers(lines, category, name)
            line_number = line_numbers[occurrence[key]] if occurrence[key] < len(line_numbers) else None
            occurrence[key] += 1
            yield _format_location(path, category, name, line_number), params

    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            lines, data = _load_config(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        platform, module = _axis_keys(data)

        has_platform = isinstance(platform, str) and bool(platform)
        has_module = isinstance(module, str) and bool(module)
        if has_platform and has_module:
            errors.append(f"{_relative(path)}: declares both tests.platform and tests.module (exactly one required)")
        elif not has_platform and not has_module:
            errors.append(f"{_relative(path)}: missing axis key (declare tests.platform or tests.module)")

        required_label = required_suite_label(platform, module)
        for _category, name, _params in _iter_checks(data):
            name_files[name].append(str(_relative(path)))
        for location, params in _located_checks(path, lines, data):
            test_id = _normalize_test_id(params.get("test_id"))
            labels = _normalize_labels(params.get("labels"))
            if test_id is None:
                errors.append(f'{location}: missing test_id (use a plan id or "N/A")')
            if not labels:
                errors.append(f"{location}: missing labels (non-empty list required)")
            elif required_label and required_label not in labels:
                errors.append(f"{location}: missing suite label {required_label!r}")
            governance_error = _multiple_platform_labels_error(location, labels, platform_labels)
            if governance_error:
                errors.append(governance_error)

    for name, files in sorted(name_files.items()):
        if len(files) > 1:
            errors.append(
                f"wiring name {name!r} appears {len(files)} times ({', '.join(sorted(set(files)))}); "
                f"give each wiring a unique variant name like {name.split('-')[0]}-<suite>_<category>"
            )

    for path in _iter_provider_configs(providers_dir):
        try:
            lines, data = _load_config(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for location, params in _located_checks(path, lines, data):
            labels = _normalize_labels(params.get("labels"))
            governance_error = _multiple_platform_labels_error(location, labels, platform_labels)
            if governance_error:
                errors.append(governance_error)

    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when any suite check is missing test_id or labels.",
    )
    args = parser.parse_args(argv)

    errors = wiring_errors()
    if errors:
        header = f"suite wiring validation failed ({len(errors)} issue(s)):"
        message = header + "\n  " + "\n  ".join(errors)
        if args.check:
            sys.stderr.write(message + "\n")
            return 1
        print(message)
        return 0

    ok = f"OK: all wired checks in {SUITES_DIR.relative_to(REPO_ROOT)} declare test_id, labels, and suite labels."
    print(ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
