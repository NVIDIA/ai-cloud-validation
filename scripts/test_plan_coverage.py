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

"""Join validation classes to docs/test-plan.yaml via the ``test_ids`` class metadata.

This is the single source of truth for "which test plan items are implemented by
which validation class", replacing brittle git/PR archaeology. It relies on each
``BaseValidation`` subclass declaring the test-plan IDs it implements via the
``test_ids`` ClassVar (surfaced through the catalog).

``--check`` runs three CI guardrails:

1. Integrity   - a class must not declare a ``test_ids`` value absent from the plan.
2. Completeness - every class must declare ``test_ids``; an empty tuple is an
   error (catches "we forgot one"). Use ``(UNMAPPED,)`` for an intentional gap.
3. Consistency - a class's labels must match the domain its ``test_ids`` imply
   (e.g. a ``K8S*`` id requires a ``kubernetes`` label) - catches mis-assignments.

Correctness beyond these heuristics needs a human: ``--review`` emits a
class -> test_id -> plan-summary table for eyeballing.

Offline: reads the committed test plan, the in-repo catalog, and the release
manifest. No network access required.

Usage:
    python3 scripts/test_plan_coverage.py            # coverage report
    python3 scripts/test_plan_coverage.py --check    # CI guardrails
    python3 scripts/test_plan_coverage.py --review review.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from isvtest.core.validation import UNMAPPED

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = REPO_ROOT / "docs" / "test-plan.yaml"
SUITES_DIR = REPO_ROOT / "isvctl" / "configs" / "suites"

# Requirement family (the alpha prefix of a test_id, e.g. "K8S22-01" -> "K8S",
# "SEC14-01" -> "SEC") -> a label the implementing class must carry. Only
# unambiguous domains are listed; families where the class labels do not encode
# the plan domain (CP, CNP, BOOT, AUTH, DMS, OBS, TELEM) are omitted to avoid
# false positives - their correctness relies on --review instead.
PREFIX_REQUIRED_LABELS: dict[str, str] = {
    "SEC": "security",
    "K8S": "kubernetes",
    "SLURM": "slurm",
    "SDN": "network",
    "NET": "network",
    "BMAAS": "bare_metal",
    "VMAAS": "vm",
}


def load_plan(path: Path = PLAN_PATH) -> dict[str, dict[str, Any]]:
    """Return a mapping of ``test_id`` to its test-plan entry."""
    data = yaml.safe_load(path.read_text())
    entries: dict[str, dict[str, Any]] = {}
    for domain in data.get("domains", []):
        for comp in domain.get("components", []):
            for cap in comp.get("capabilities", []):
                for test in cap.get("tests", []):
                    tid = test.get("test_id")
                    if tid:
                        entries[tid] = test
    return entries


def catalog_entries() -> list[dict[str, Any]]:
    """Return all catalog entries (released + unreleased) with name/labels/test_ids."""
    from isvtest.catalog import build_catalog

    return build_catalog(released_only=False)


def real_test_ids(entry: dict[str, Any]) -> list[str]:
    """Return an entry's declared test IDs excluding the ``UNMAPPED`` sentinel."""
    return [t for t in (entry.get("test_ids") or []) if t != UNMAPPED]


def _iter_check_items(cat_config: Any) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(check_name, params)`` pairs from a validation category config.

    Handles the group-defaults form (``{step, checks: {...}|[...]}``) and the
    bare list form, mirroring catalog._extract_checks_from_config.
    """
    items: list[tuple[str, dict[str, Any]]] = []

    def _add(mapping: Any) -> None:
        if isinstance(mapping, dict):
            for name, params in mapping.items():
                items.append((name, params if isinstance(params, dict) else {}))

    if isinstance(cat_config, dict) and "checks" in cat_config:
        checks_val = cat_config["checks"]
        if isinstance(checks_val, dict):
            _add(checks_val)
        elif isinstance(checks_val, list):
            for entry in checks_val:
                _add(entry)
    elif isinstance(cat_config, list):
        for entry in cat_config:
            _add(entry)
    return items


def config_test_id_map(suites_dir: Path = SUITES_DIR) -> dict[str, list[str]]:
    """Return ``check_name -> test_ids`` declared inline in the suite configs.

    Under the YAML model the (check, context) wiring is the source of truth, so
    coverage reads the singular ``test_id`` straight from each check's params.
    A given check name may appear in several suites (e.g. ConnectivityCheck in
    both bare_metal and vm), so values still aggregate to a set across configs.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        validations = (data.get("tests") or {}).get("validations") or {}
        for cat_config in validations.values():
            for name, params in _iter_check_items(cat_config):
                tid = params.get("test_id")
                if isinstance(tid, str) and tid:
                    out[name].add(tid)
    return {name: sorted(ids) for name, ids in out.items()}


def apply_config_test_ids(
    entries: list[dict[str, Any]], config_map: dict[str, list[str]] | None = None
) -> list[dict[str, Any]]:
    """Return entries whose ``test_ids`` are unioned with config-declared ids.

    During the pilot only some checks live in YAML, so this merges the two
    sources; the end-state (all ids in YAML) is a no-op union over empty class
    metadata.
    """
    config_map = config_test_id_map() if config_map is None else config_map
    merged: list[dict[str, Any]] = []
    for entry in entries:
        cfg_ids = config_map.get(entry["name"], [])
        if cfg_ids:
            union = sorted(set(entry.get("test_ids") or []) | set(cfg_ids))
            entry = {**entry, "test_ids": union}
        merged.append(entry)
    return merged


def class_test_id_map(entries: list[dict[str, Any]] | None = None) -> dict[str, list[str]]:
    """Return a mapping of class/variant name to its real (non-sentinel) test IDs."""
    entries = catalog_entries() if entries is None else entries
    return {e["name"]: real_test_ids(e) for e in entries if real_test_ids(e)}


def released_names() -> set[str]:
    """Return the set of released validation class names."""
    from isvtest.release_manifest import load_released_tests

    return load_released_tests()


def _is_released(name: str, released: set[str]) -> bool:
    """Return whether ``name`` (or its variant base ``Name-suffix``) is released."""
    return name in released or name.split("-")[0] in released


def integrity_errors(plan_ids: set[str], class_map: dict[str, list[str]]) -> list[str]:
    """Errors for classes declaring a ``test_id`` absent from the plan (typo/stale)."""
    errors: list[str] = []
    for name in sorted(class_map):
        for tid in class_map[name]:
            if tid not in plan_ids:
                errors.append(f"{name}: declares unknown test_id {tid!r} (not in test-plan.yaml)")
    return errors


def completeness_errors(entries: list[dict[str, Any]]) -> list[str]:
    """Errors for classes that declare no ``test_ids`` at all.

    Every validation must make an explicit choice: link it to a plan id, or
    declare ``(UNMAPPED,)`` for an intentional gap (generic check / no plan
    entry). An empty ``test_ids`` means "not yet linked" and is an error, so a
    new check can never silently slip through.
    """
    errors: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        base = entry["name"].split("-")[0]
        if base in seen or entry.get("test_ids"):
            continue
        seen.add(base)
        errors.append(
            f"{entry['name']}: declares no test_ids - link it to a test-plan id, "
            "or set (UNMAPPED,) for an intentional gap"
        )
    return sorted(errors)


def _req_family(test_id: str) -> str:
    """Return the alpha requirement family of a test_id ("K8S22-01" -> "K8S")."""
    return re.sub(r"\d+$", "", test_id.split("-")[0])


def consistency_errors(entries: list[dict[str, Any]]) -> list[str]:
    """Errors where a class's labels do not match the domain its ``test_ids`` imply."""
    errors: list[str] = []
    for entry in entries:
        labels = set(entry.get("labels") or [])
        for tid in real_test_ids(entry):
            required = PREFIX_REQUIRED_LABELS.get(_req_family(tid))
            if required and required not in labels:
                errors.append(
                    f"{entry['name']}: test_id {tid} implies label {required!r}, but class labels are {sorted(labels)}"
                )
    return sorted(errors)


def build_coverage(
    plan_entries: dict[str, dict[str, Any]],
    class_map: dict[str, list[str]],
    released: set[str],
) -> dict[str, Any]:
    """Compute coverage statistics joining the plan, classes, and release manifest."""
    test_id_to_classes: dict[str, list[str]] = defaultdict(list)
    for name, tids in class_map.items():
        for tid in tids:
            test_id_to_classes[tid].append(name)

    covered = {t for t in plan_entries if test_id_to_classes.get(t)}
    covered_released = {
        t for t in plan_entries if any(_is_released(c, released) for c in test_id_to_classes.get(t, []))
    }

    return {
        "plan_test_ids": len(plan_entries),
        "plan_test_ids_covered": len(covered),
        "plan_test_ids_covered_by_released_class": len(covered_released),
        "classes_with_test_ids": len(class_map),
        "test_id_to_classes": {t: sorted(c) for t, c in sorted(test_id_to_classes.items())},
    }


def render_markdown(coverage: dict[str, Any], plan_entries: dict[str, dict[str, Any]]) -> str:
    """Render the coverage report as Markdown."""
    lines = [
        "# Test-plan coverage (via class `test_ids`)",
        "",
        f"- Test-plan items: **{coverage['plan_test_ids']}**",
        f"- Covered by \u22651 class: **{coverage['plan_test_ids_covered']}**",
        f"- Covered by a released class: **{coverage['plan_test_ids_covered_by_released_class']}**",
        f"- Validation classes declaring `test_ids`: **{coverage['classes_with_test_ids']}**",
        "",
        "## Covered test IDs",
        "",
        "| Test ID | Req | Implementing class(es) |",
        "|---|---|---|",
    ]
    for tid, classes in coverage["test_id_to_classes"].items():
        entry = plan_entries.get(tid, {})
        req = entry.get("req_id", "")
        lines.append(f"| `{tid}` | {req} | {', '.join(f'`{c}`' for c in classes)} |")
    return "\n".join(lines) + "\n"


def render_review(entries: list[dict[str, Any]], plan_entries: dict[str, dict[str, Any]]) -> str:
    """Render a class -> test_id -> plan-summary table for human correctness review."""
    lines = [
        "# test_ids review (class \u2192 plan)",
        "",
        "Eyeball that each class's `test_ids` summaries match what the class checks.",
        "",
        "| Class | Labels | test_ids | Plan summaries |",
        "|---|---|---|---|",
    ]
    for entry in sorted(entries, key=lambda e: e["name"]):
        tids = real_test_ids(entry)
        if not tids:
            continue
        labels = ", ".join(entry.get("labels") or [])
        summaries = " <br> ".join(f"{t}: {(plan_entries.get(t, {}).get('summary') or '')[:60]}" for t in tids)
        lines.append(f"| `{entry['name']}` | {labels} | {', '.join(tids)} | {summaries} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI guardrails: fail on integrity, completeness, or consistency errors.",
    )
    parser.add_argument("--json", metavar="PATH", help="Write the coverage report as JSON to PATH.")
    parser.add_argument("--markdown", metavar="PATH", help="Write the coverage report as Markdown to PATH.")
    parser.add_argument("--review", metavar="PATH", help="Write the class->plan review table as Markdown to PATH.")
    args = parser.parse_args(argv)

    plan_entries = load_plan()
    plan_ids = set(plan_entries)
    entries = apply_config_test_ids(catalog_entries())
    class_map = class_test_id_map(entries)

    checks = {
        "integrity": integrity_errors(plan_ids, class_map),
        "completeness": completeness_errors(entries),
        "consistency": consistency_errors(entries),
    }
    all_errors = [f"[{kind}] {msg}" for kind, msgs in checks.items() for msg in msgs]

    if args.check:
        if all_errors:
            sys.stderr.write("test-plan coverage check failed:\n  " + "\n  ".join(all_errors) + "\n")
            return 1
        print(f"OK: {len(class_map)} mapped classes pass integrity, completeness, and consistency.")
        return 0

    if all_errors:
        sys.stderr.write("WARNING (run with --check in CI):\n  " + "\n  ".join(all_errors) + "\n")

    coverage = build_coverage(plan_entries, class_map, released_names())

    if args.json:
        Path(args.json).write_text(json.dumps(coverage, indent=2) + "\n")
        print(f"Wrote {args.json}")
    if args.markdown:
        Path(args.markdown).write_text(render_markdown(coverage, plan_entries))
        print(f"Wrote {args.markdown}")
    if args.review:
        Path(args.review).write_text(render_review(entries, plan_entries))
        print(f"Wrote {args.review}")

    print(f"Test-plan items:                     {coverage['plan_test_ids']}")
    print(f"Covered by >=1 class:                 {coverage['plan_test_ids_covered']}")
    print(f"Covered by a released class:          {coverage['plan_test_ids_covered_by_released_class']}")
    print(f"Classes declaring test_ids:          {coverage['classes_with_test_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
