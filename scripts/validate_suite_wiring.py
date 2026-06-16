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

"""Require ``test_id`` and ``labels`` on every check wired in suite YAML.

Suite configs under ``isvctl/configs/suites/`` are the source of truth for
validation metadata on this branch. Each wired check must declare:

* ``test_id`` - a plan id from ``docs/test-plan.yaml``, or ``"N/A"`` when the
  check is generic plumbing with no plan item.
* ``labels`` - a non-empty list used for pytest selection and catalog reporting.

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


def iter_suite_checks(config_path: Path) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(category, check_name, params)`` for checks in a suite file."""
    try:
        data = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read/parse {config_path}: {exc}") from exc

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

    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            lines = path.read_text().splitlines()
            checks = list(iter_suite_checks(path))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for category, name, params in checks:
            key = (path, category, name)
            line_numbers = find_check_line_numbers(lines, category, name)
            line_number = line_numbers[occurrence[key]] if occurrence[key] < len(line_numbers) else None
            occurrence[key] += 1

            location = _format_location(path, category, name, line_number)
            test_id = _normalize_test_id(params.get("test_id"))
            labels = _normalize_labels(params.get("labels"))
            if test_id is None:
                errors.append(f'{location}: missing test_id (use a plan id or "N/A")')
            if not labels:
                errors.append(f"{location}: missing labels (non-empty list required)")
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

    ok = f"OK: all wired checks in {SUITES_DIR.relative_to(REPO_ROOT)} declare test_id and labels."
    print(ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
