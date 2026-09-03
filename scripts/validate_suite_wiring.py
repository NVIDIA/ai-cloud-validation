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

"""Validate suite identity and check resolution in canonical and provider YAML.

Suite configs recursively under ``isvctl/configs/suites/`` are the source of truth for
validation metadata on this branch. Each wired check must declare:

* ``test_id`` - a plan id from ``docs/test-plan.yaml``, or ``"N/A"`` when the
  check is generic plumbing with no plan item.
* ``labels`` - a non-empty list used for pytest selection and catalog reporting.
  Each canonical suite check must include its suite label, for example checks in
  ``bare_metal.yaml`` must include ``bare_metal``.

A check wired with ``compose`` is a composite: it names no validation class of
its own, so it must supply the ``description`` the catalog would otherwise take
from a class, and every name in its ``compose`` list must be a real check.

Checks marked ``compose_only`` (``StepSuccessCheck`` and friends) assert
something generic, so their class name would be a poor catalog identity. They
may only be reached from inside a ``compose`` list.

Provider configs are validated after resolving their imports. This catches
overrides that accidentally replace a canonical composite's metadata, as well
as provider-only checks that bypass the canonical suite guardrails.

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
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from isvctl.config.merger import merge_yaml_files
from isvtest.catalog import iter_checks_from_data
from isvtest.core.composite import COMPOSE_KEY, composed_members, is_composite
from isvtest.core.discovery import discover_all_tests
from isvtest.core.resolution import (
    DECLARABLE_CAPABILITIES,
    canonical_suite_name,
    parse_validations,
    requires_error,
    resolve_class_key,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITES_DIR = REPO_ROOT / "isvctl" / "configs" / "suites"
PROVIDERS_DIR = REPO_ROOT / "isvctl" / "configs" / "providers"
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


def required_suite_label(config_path: Path) -> str | None:
    """Return the label every check in a known canonical suite must carry."""
    return canonical_suite_name(config_path.stem)


@cache
def discovered_check_names() -> frozenset[str]:
    """Return the names of every discoverable validation class."""
    return frozenset(cls.__name__ for cls in discover_all_tests())


@cache
def compose_only_check_names() -> frozenset[str]:
    """Return the checks that may only be reached from inside a composite."""
    return frozenset(cls.__name__ for cls in discover_all_tests() if getattr(cls, "compose_only", False))


def composite_errors(location: str, name: str, params: dict[str, Any]) -> list[str]:
    """Return errors for a check wired with ``compose``.

    A composite borrows nothing from a validation class, so what a class would
    have supplied - a name of its own and a description - has to be declared and
    checked here instead.
    """
    known = discovered_check_names()
    errors: list[str] = []

    if name in known:
        errors.append(f"{location}: composite name shadows validation class {name!r}; give the composite its own name")
    if not isinstance(params.get("description"), str) or not params["description"].strip():
        errors.append(f"{location}: composite requires a description (it becomes the catalog description)")

    raw = params[COMPOSE_KEY]
    if not isinstance(raw, list) or not raw:
        errors.append(f"{location}: {COMPOSE_KEY} must be a non-empty list of check names")
        return errors

    members = composed_members(raw)
    if len(members) != len(raw):
        errors.append(f"{location}: each {COMPOSE_KEY} item must be 'CheckName' or 'CheckName: {{params}}'")
    for member_name, _ in members:
        if member_name not in known:
            errors.append(f"{location}: {COMPOSE_KEY} names unknown check {member_name!r}")
    return errors


def iter_suite_checks(config_path: Path) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(category, check_name, params)`` for checks in a suite file."""
    try:
        data = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read/parse {config_path}: {exc}") from exc
    yield from iter_checks_from_data(data)


def _format_location(config_path: Path, category: str, check_name: str, line_number: int | None) -> str:
    """Return a stable location string for error messages."""
    try:
        rel_path = config_path.relative_to(REPO_ROOT)
    except ValueError:
        rel_path = config_path
    if line_number is None:
        return f"{rel_path} → {category} → {check_name}"
    return f"{rel_path}:{line_number} → {category} → {check_name}"


def wiring_errors(suites_dir: Path = SUITES_DIR) -> list[str]:
    """Return human-readable errors for incomplete suite check wiring."""
    errors: list[str] = []
    occurrence: dict[tuple[Path, str, str], int] = defaultdict(int)
    wiring_locations: dict[str, str] = {}

    # Read and parse each suite once; both the dead-requirement pre-pass and the
    # per-check loop below work off these parsed documents.
    parsed: list[tuple[Path, list[str], dict[str, Any]]] = []
    for path in sorted(suites_dir.rglob("*.yaml")):
        try:
            text = path.read_text()
            parsed.append((path, text.splitlines(), yaml.safe_load(text) or {}))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"failed to read/parse {path}: {exc}")

    # A `requires` value is only satisfiable if an ISV can declare that
    # capability, which requires a platform suite to exist for it. Collect the
    # capabilities that actually have a suite so unreachable (dead)
    # requirements can be flagged below.
    declared_capabilities: set[str] = set()
    for _, _, data in parsed:
        tests = data.get("tests") if isinstance(data, dict) else None
        capability = tests.get("capability") if isinstance(tests, dict) else None
        if isinstance(capability, str) and capability in DECLARABLE_CAPABILITIES:
            declared_capabilities.add(capability)

    for path, lines, data in parsed:
        try:
            checks = list(iter_checks_from_data(data))
        except (ValueError, AttributeError) as exc:
            errors.append(f"failed to read/parse {path}: {exc}")
            continue
        tests = data.get("tests") or {}
        capability = tests.get("capability") if isinstance(tests, dict) else None
        module = tests.get("module") if isinstance(tests, dict) else None
        if module is not None:
            errors.append(f"{path}: tests.module is no longer supported")
        if isinstance(tests, dict) and tests.get("platform") is not None:
            errors.append(f"{path}: tests.platform was renamed to tests.capability")
        if capability is not None and capability not in DECLARABLE_CAPABILITIES:
            errors.append(f"{path}: tests.capability must be one of: {', '.join(sorted(DECLARABLE_CAPABILITIES))}")
        suite_is_platform = isinstance(capability, str) and capability in DECLARABLE_CAPABILITIES
        if not suite_is_platform:
            suite_name = canonical_suite_name(path.stem)
            if suite_name in DECLARABLE_CAPABILITIES:
                errors.append(
                    f"{path}: plain suite name {suite_name!r} collides with a declarable "
                    "capability; rename the file so capability and suite namespaces stay disjoint"
                )
        for category, name, params in checks:
            key = (path, category, name)
            line_numbers = find_check_line_numbers(lines, category, name)
            line_number = line_numbers[occurrence[key]] if occurrence[key] < len(line_numbers) else None
            occurrence[key] += 1

            location = _format_location(path, category, name, line_number)
            test_id = _normalize_test_id(params.get("test_id"))
            labels = _normalize_labels(params.get("labels"))
            required_label = required_suite_label(path)
            previous_location = wiring_locations.get(name)
            if previous_location:
                errors.append(f"{location}: wiring name is not globally unique (also at {previous_location})")
            else:
                wiring_locations[name] = location
            if is_composite(params):
                errors.extend(composite_errors(location, name, params))
            elif generic := resolve_class_key(name, compose_only_check_names()):
                errors.append(
                    f"{location}: {generic} is a generic check and may only appear in a composite's "
                    f"{COMPOSE_KEY} list; name what the test proves and compose it"
                )
            if test_id is None:
                errors.append(f'{location}: missing test_id (use a plan id or "N/A")')
            if not labels:
                errors.append(f"{location}: missing labels (non-empty list required)")
            elif required_label and required_label not in labels:
                errors.append(f"{location}: missing suite label {required_label!r}")
            if "platforms" in params:
                errors.append(f"{location}: legacy platforms is not supported; use requires in plain suites")
            if capability:
                if "requires" in params:
                    errors.append(f"{location}: requires is not allowed in platform suites")
            else:
                requires = params.get("requires")
                if not isinstance(requires, list):
                    errors.append(f"{location}: missing requires (use [] for core checks)")
                elif message := requires_error(requires):
                    errors.append(f"{location}: {message}")
                else:
                    dead = sorted(set(requires) - declared_capabilities)
                    if dead:
                        errors.append(
                            f"{location}: requires names {', '.join(dead)} which has no platform "
                            "suite; no ISV can declare it, so the check is unreachable"
                        )
    return errors


def provider_wiring_errors(providers_dir: Path = PROVIDERS_DIR) -> list[str]:
    """Return errors in provider validations after imports and overrides merge."""
    errors: list[str] = []
    known = discovered_check_names()
    generic_names = compose_only_check_names()

    for path in sorted(providers_dir.rglob("*.yaml")):
        try:
            data = merge_yaml_files([path])
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"failed to read/merge {path}: {exc}")
            continue

        tests = data.get("tests") if isinstance(data, dict) else None
        validations = tests.get("validations") if isinstance(tests, dict) else None
        if not isinstance(validations, dict):
            continue

        try:
            entries = parse_validations(validations)
        except (ValueError, AttributeError) as exc:
            errors.append(f"failed to parse merged validations in {path}: {exc}")
            continue

        locations: dict[str, str] = {}
        for entry in entries:
            location = _format_location(path, entry.category, entry.name, None)
            previous_location = locations.get(entry.name)
            if previous_location:
                errors.append(
                    f"{location}: wiring name is not unique in merged provider config (also at {previous_location})"
                )
            else:
                locations[entry.name] = location

            params = entry.params_template
            if not isinstance(params, dict):
                errors.append(f"{location}: check parameters must be a mapping")
                continue
            if is_composite(params):
                errors.extend(composite_errors(location, entry.name, params))
            elif generic := resolve_class_key(entry.name, generic_names):
                errors.append(
                    f"{location}: {generic} is a generic check and may only appear in a composite's "
                    f"{COMPOSE_KEY} list; name what the test proves and compose it"
                )
            elif resolve_class_key(entry.name, known) is None:
                errors.append(
                    f"{location}: names unknown validation {entry.name!r}; "
                    "an override may have replaced composite metadata"
                )

    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit 1 on wiring violations (missing test_id/labels, unregistered suites, "
            "or isvreporter platform mismatches)."
        ),
    )
    args = parser.parse_args(argv)

    errors = [*wiring_errors(), *provider_wiring_errors()]
    if errors:
        header = f"suite wiring validation failed ({len(errors)} issue(s)):"
        message = header + "\n  " + "\n  ".join(errors)
        if args.check:
            sys.stderr.write(message + "\n")
            return 1
        print(message)
        return 0

    ok = (
        f"OK: canonical checks in {SUITES_DIR.relative_to(REPO_ROOT)} have valid metadata "
        f"and merged configs in {PROVIDERS_DIR.relative_to(REPO_ROOT)} resolve."
    )
    print(ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
