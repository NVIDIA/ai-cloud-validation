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

"""Govern the capability x module taxonomy wired in suite YAML.

Suite configs under ``isvctl/configs/suites/`` are the source of truth for
validation metadata on this branch. This validator enforces:

* ``tests.capability`` / ``tests.module`` - every suite declares exactly one of
  these axis keys (its value is also the runtime platform). The capability/module
  *label* axes are derived from these keys (so adding a suite extends the axes
  automatically).
* ``test_id`` - a plan id from ``docs/test-plan.yaml``, or ``"N/A"`` when the
  check is generic plumbing with no plan item.
* ``labels`` - a non-empty list used for pytest selection and catalog reporting.
  Each canonical suite check must include its suite label, for example checks in
  ``bare_metal.yaml`` must include ``bare_metal``.
* label governance - every label used in wiring must be a known capability,
  module, or modifier label (kills typos and ungoverned growth), and a check
  may carry at most one capability-axis label (capability-scoped exclusion is
  any-intersection, so two capability labels would skip the check under every
  column).

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

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITES_DIR = REPO_ROOT / "isvctl" / "configs" / "suites"
_NEXT_CATEGORY_LINE = re.compile(r"^    \S")
SUITE_REQUIRED_LABELS: dict[str, str] = {
    "bare_metal": "bare_metal",
    "control-plane": "control_plane",
    "iam": "iam",
    "image-registry": "image_registry",
    "k8s": "kubernetes",
    "network": "network",
    "observability": "observability",
    "security": "security",
    "slurm": "slurm",
    "storage": "storage",
    "vm": "vm",
}

# Labels that are neither capability nor module axes: hardware/trait attributes
# and selection presets. Authoritative list generated from
# ``ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl catalog labels --json`` minus the
# derived axis labels. Any new label must be added here (a modifier) or become a
# capability/module suite platform, otherwise wiring validation fails.
MODIFIER_LABELS: frozenset[str] = frozenset(
    {
        "min_req",
        "slow",
        "workload",
        "gpu",
        "ssh",
        "sanitization",
        "attestation",
        "firmware",
        "disk",
        "infiniband",
        "ingestion",
        "dpu",
        "governance",
        "health",
        "capacity",
        "l2",
    }
)

# Module-axis labels with no dedicated ``kind: module`` suite yet: their checks
# piggyback on a capability suite (e.g. K8s CSI ``storage`` checks live inline in
# ``k8s.yaml``). Allowlisted so wiring can select them by label.
EXTRA_MODULE_LABELS: frozenset[str] = frozenset({"storage"})


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
    return SUITE_REQUIRED_LABELS.get(config_path.stem)


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


def _suite_capability_and_module(config_path: Path) -> tuple[Any, Any]:
    """Return the ``(capability, module)`` axis keys declared in a suite's ``tests:`` block."""
    try:
        data = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read/parse {config_path}: {exc}") from exc
    tests = (data or {}).get("tests", {})
    if not isinstance(tests, dict):
        return None, None
    return tests.get("capability"), tests.get("module")


def derive_axis_labels(suites_dir: Path = SUITES_DIR) -> tuple[frozenset[str], frozenset[str]]:
    """Derive the (capability, module) label axes from the suites' axis keys.

    Capability labels are the values of ``capability:`` keys; module labels are
    the values of ``module:`` keys plus the piggyback allowlist
    (:data:`EXTRA_MODULE_LABELS`). Malformed suites are skipped here;
    :func:`wiring_errors` reports them separately.
    """
    capability: set[str] = set()
    module: set[str] = set()
    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            cap, mod = _suite_capability_and_module(path)
        except ValueError:
            continue
        if isinstance(cap, str) and cap:
            capability.add(cap)
        if isinstance(mod, str) and mod:
            module.add(mod)
    return frozenset(capability), frozenset(module | EXTRA_MODULE_LABELS)


def _iter_provider_configs(providers_dir: Path) -> Iterator[Path]:
    """Yield provider config YAML files (``providers/*/config/*.yaml`` + ``providers/*.yaml``)."""
    if not providers_dir.is_dir():
        return
    yield from sorted(providers_dir.glob("*/config/*.yaml"))
    yield from sorted(providers_dir.glob("*.yaml"))


def _label_governance_errors(
    location: str,
    labels: list[str],
    capability_labels: frozenset[str],
    known_labels: frozenset[str],
) -> list[str]:
    """Return unknown-label and multiple-capability-label errors for one check."""
    errors: list[str] = []
    unknown = [label for label in labels if label not in known_labels]
    for label in unknown:
        errors.append(f"{location}: unknown label {label!r} (not a capability, module, or modifier label)")
    capability_hits = sorted({label for label in labels if label in capability_labels})
    if len(capability_hits) > 1:
        errors.append(f"{location}: multiple capability labels ({', '.join(capability_hits)}); at most one is allowed")
    return errors


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
    The capability/module label axes are derived from ``suites_dir``.
    """
    if providers_dir is None:
        providers_dir = suites_dir.parent / "providers"

    capability_labels, module_labels = derive_axis_labels(suites_dir)
    known_labels = capability_labels | module_labels | MODIFIER_LABELS

    errors: list[str] = []
    occurrence: dict[tuple[Path, str, str], int] = defaultdict(int)

    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            lines = path.read_text().splitlines()
            checks = list(iter_suite_checks(path))
            capability, module = _suite_capability_and_module(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        has_capability = isinstance(capability, str) and bool(capability)
        has_module = isinstance(module, str) and bool(module)
        if has_capability and has_module:
            errors.append(
                f"{_relative(path)}: declares both tests.capability and tests.module (exactly one required)"
            )
        elif not has_capability and not has_module:
            errors.append(
                f"{_relative(path)}: missing axis key (declare tests.capability or tests.module)"
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
            if test_id is None:
                errors.append(f'{location}: missing test_id (use a plan id or "N/A")')
            if not labels:
                errors.append(f"{location}: missing labels (non-empty list required)")
            elif required_label and required_label not in labels:
                errors.append(f"{location}: missing suite label {required_label!r}")
            errors.extend(_label_governance_errors(location, labels, capability_labels, known_labels))

    for path in _iter_provider_configs(providers_dir):
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
            labels = _normalize_labels(params.get("labels"))
            errors.extend(_label_governance_errors(location, labels, capability_labels, known_labels))

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
