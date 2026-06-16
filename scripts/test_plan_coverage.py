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

"""Join validation checks to docs/test-plan.yaml via their wired ``test_id``s.

This is the single source of truth for "which test plan items are implemented by
which check", replacing brittle git/PR archaeology. Test IDs live on the per-check
YAML wiring (``test_id:``); this script reads them from the suite configs and joins
them to the plan.

``--check`` runs two CI guardrails:

1. Integrity   - a declared ``test_id`` must exist in the plan (catches typos/stale ids).
2. Consistency - a check's labels must match the domain its ``test_id`` implies
   (e.g. a ``K8S*`` id requires a ``kubernetes`` label) - catches mis-assignments.

There is intentionally no completeness guardrail: a check with no ``test_id``
is allowed (it simply contributes nothing to coverage). This trades away the
"someone forgot to map a new check" tripwire; reintroduce it later by exempting
a known generic set and requiring a ``test_id`` on everything else.

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
from isvtest.catalog import iter_config_checks

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = REPO_ROOT / "docs" / "test-plan.yaml"
SUITES_DIR = REPO_ROOT / "isvctl" / "configs" / "suites"

# YAML sentinel: a wired check that intentionally maps to no test id declares
# ``test_id: "N/A"``. It is stripped from coverage so it counts as "no mapping"
# without being mistaken for a real plan id (which would fail the integrity check).
UNMAPPED = "N/A"

# Requirement family (the alpha prefix of a test_id, e.g. "K8S22-01" -> "K8S",
# "SEC14-01" -> "SEC") -> a label the implementing check must carry. Only
# unambiguous domains are listed; families where the wiring labels do not encode
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
    """Return a mapping of ``test_id`` to its test-plan entry.

    A ``test_id`` is a unique identifier, so a repeated one in the plan is an
    authoring error that would silently shadow an earlier entry and corrupt every
    derived report. Fail loudly rather than letting the last write win.
    """
    data = yaml.safe_load(path.read_text())
    entries: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for domain in data.get("domains", []):
        for comp in domain.get("components", []):
            for cap in comp.get("capabilities", []):
                for test in cap.get("tests", []):
                    tid = test.get("test_id")
                    if not tid:
                        continue
                    if tid in entries:
                        duplicates.append(tid)
                    else:
                        entries[tid] = test
    if duplicates:
        joined = ", ".join(sorted(set(duplicates)))
        raise SystemExit(f"duplicate test_id(s) in {path.name}: {joined}")
    return entries


def catalog_entries() -> list[dict[str, Any]]:
    """Return all catalog entries (released + unreleased) with name/labels/test_ids."""
    # Deferred: importing build_catalog pulls in validation discovery (~1s).
    from isvtest.catalog import build_catalog

    return build_catalog(released_only=False)


def real_test_ids(entry: dict[str, Any]) -> list[str]:
    """Return an entry's declared test IDs excluding the ``UNMAPPED`` sentinel."""
    return [t for t in (entry.get("test_ids") or []) if t != UNMAPPED]


def config_test_id_map(suites_dir: Path = SUITES_DIR) -> dict[str, list[str]]:
    """Return ``check_name -> test_ids`` declared inline in the suite configs.

    Under the YAML model the (check, context) wiring is the source of truth, so
    coverage reads the singular ``test_id`` straight from each check's params.
    A given check name may appear in several suites (e.g. ConnectivityCheck in
    both bare_metal and vm), so values still aggregate to a set across configs.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for path in sorted(suites_dir.glob("*.yaml")):
        for name, params in iter_config_checks(path):
            tid = params.get("test_id")
            if isinstance(tid, str) and tid:
                out[name].add(tid)
    return {name: sorted(ids) for name, ids in out.items()}


def config_label_map(suites_dir: Path = SUITES_DIR) -> dict[str, list[str]]:
    """Return ``check_name -> labels`` declared inline in the suite configs.

    Mirrors :func:`config_test_id_map` for the ``labels`` key. Labels live on
    the (check, context) wiring, so coverage's domain-consistency guardrail reads
    them from YAML. A check name appearing in several suites aggregates to the
    union of its labels.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for path in sorted(suites_dir.glob("*.yaml")):
        for name, params in iter_config_checks(path):
            labels = params.get("labels")
            if isinstance(labels, str):
                labels = [labels]
            if isinstance(labels, list):
                out[name].update(label for label in labels if isinstance(label, str) and label)
    return {name: sorted(labels) for name, labels in out.items()}


def _variant_base(name: str) -> str:
    """Return the base class name for a variant entry ("SlurmPartition-cpu" -> "SlurmPartition")."""
    return name.split("-")[0]


def _apply_variant_union(
    entries: list[dict[str, Any]], value_map: dict[str, list[str]], attr: str
) -> list[dict[str, Any]]:
    """Union ``value_map`` onto each entry's ``attr`` list, propagating variants.

    Some checks are wired only as variants (e.g. ``SlurmPartition-cpu``,
    ``SlurmPartition-gpu``) while the catalog also carries the bare base class
    (``SlurmPartition``). A variant's values are propagated up to its base so the
    base entry is not orphaned once its class-level metadata is removed; variant
    entries keep only their own values to preserve per-wiring precision.
    """
    base_union: dict[str, set[str]] = defaultdict(set)
    for name, values in value_map.items():
        base_union[_variant_base(name)].update(values)

    merged: list[dict[str, Any]] = []
    for entry in entries:
        name = entry["name"]
        cfg_values = set(value_map.get(name, []))
        if name == _variant_base(name):
            cfg_values |= base_union.get(name, set())
        if cfg_values:
            entry = {**entry, attr: sorted(set(entry.get(attr) or []) | cfg_values)}
        merged.append(entry)
    return merged


def apply_config_labels(
    entries: list[dict[str, Any]], label_map: dict[str, list[str]] | None = None
) -> list[dict[str, Any]]:
    """Return entries whose ``labels`` are unioned with config-declared labels."""
    label_map = config_label_map() if label_map is None else label_map
    return _apply_variant_union(entries, label_map, "labels")


def apply_config_test_ids(
    entries: list[dict[str, Any]], config_map: dict[str, list[str]] | None = None
) -> list[dict[str, Any]]:
    """Return entries whose ``test_ids`` are unioned with config-declared ids."""
    config_map = config_test_id_map() if config_map is None else config_map
    return _apply_variant_union(entries, config_map, "test_ids")


def entries_from_config_maps(
    test_id_map: dict[str, list[str]] | None = None,
    label_map: dict[str, list[str]] | None = None,
    seed_entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge suite YAML wiring onto catalog entries or YAML-only seeds.

    When ``seed_entries`` is omitted, build one stub entry per wired check name
    from the suite configs. That path is enough for ``--check`` guardrails and
    avoids importing the full validation catalog.
    """
    test_id_map = config_test_id_map() if test_id_map is None else test_id_map
    label_map = config_label_map() if label_map is None else label_map
    if seed_entries is None:
        seed_entries = [
            {"name": name, "test_ids": [], "labels": []} for name in sorted(set(test_id_map) | set(label_map))
        ]
    return apply_config_labels(apply_config_test_ids(seed_entries, test_id_map), label_map)


def class_test_id_map(entries: list[dict[str, Any]] | None = None) -> dict[str, list[str]]:
    """Return a mapping of class/variant name to its real (non-sentinel) test IDs."""
    entries = catalog_entries() if entries is None else entries
    return {e["name"]: real_test_ids(e) for e in entries if real_test_ids(e)}


def released_names() -> set[str]:
    """Return the set of released validation class names."""
    # Deferred: only needed for coverage reports, not for --check guardrails.
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
                    f"{entry['name']}: test_id {tid} implies label {required!r}, but wiring labels are {sorted(labels)}"
                )
    return sorted(errors)


def run_guardrails(
    plan_ids: set[str],
    entries: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Return integrity and consistency errors for ``entries`` against ``plan_ids``."""
    class_map = class_test_id_map(entries)
    return {
        "integrity": integrity_errors(plan_ids, class_map),
        "consistency": consistency_errors(entries),
    }


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

    uncovered = [_plan_item_summary(tid, plan_entries[tid]) for tid in plan_entries if tid not in covered]
    # Surface scheduled, high-priority gaps first; unscheduled items (blank
    # milestone/priority) sort last via the "~" sentinel (sorts after digits).
    uncovered.sort(key=lambda i: (i["milestone"] or "~", i["priority"] or "~", i["req_id"], i["test_id"]))

    return {
        "plan_test_ids": len(plan_entries),
        "plan_test_ids_covered": len(covered),
        "plan_test_ids_covered_by_released_class": len(covered_released),
        "classes_with_test_ids": len(class_map),
        "test_id_to_classes": {t: sorted(c) for t, c in sorted(test_id_to_classes.items())},
        "uncovered": uncovered,
    }


def _plan_item_summary(test_id: str, entry: dict[str, Any]) -> dict[str, str]:
    """Extract the fields used to describe an (un)covered plan item."""
    return {
        "test_id": test_id,
        "req_id": entry.get("req_id", "") or "",
        "summary": entry.get("summary", "") or "",
        "milestone": entry.get("milestone", "") or "",
        "priority": entry.get("priority", "") or "",
    }


def render_markdown(coverage: dict[str, Any], plan_entries: dict[str, dict[str, Any]]) -> str:
    """Render the coverage report as Markdown."""
    lines = [
        "# Test-plan coverage (via wired `test_id`s)",
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

    uncovered = coverage.get("uncovered", [])
    lines += [
        "",
        f"## Uncovered test IDs ({len(uncovered)})",
        "",
        "Plan items with no implementing check - the gap to close.",
        "",
        "| Test ID | Req | Milestone | Priority | Summary |",
        "|---|---|---|---|---|",
    ]
    for item in uncovered:
        lines.append(
            f"| `{item['test_id']}` | {item['req_id']} | {item['milestone']} | {item['priority']} | {item['summary']} |"
        )
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
        help="CI guardrails: fail on integrity or consistency errors.",
    )
    parser.add_argument("--json", metavar="PATH", help="Write the coverage report as JSON to PATH.")
    parser.add_argument("--markdown", metavar="PATH", help="Write the coverage report as Markdown to PATH.")
    parser.add_argument("--review", metavar="PATH", help="Write the class->plan review table as Markdown to PATH.")
    args = parser.parse_args(argv)

    plan_entries = load_plan()
    plan_ids = set(plan_entries)
    if args.check:
        entries = entries_from_config_maps()
    else:
        entries = entries_from_config_maps(seed_entries=catalog_entries())

    checks = run_guardrails(plan_ids, entries)
    class_map = class_test_id_map(entries)
    all_errors = [f"[{kind}] {msg}" for kind, msgs in checks.items() for msg in msgs]

    if args.check:
        if all_errors:
            sys.stderr.write("test-plan coverage check failed:\n  " + "\n  ".join(all_errors) + "\n")
            return 1
        print(f"OK: {len(class_map)} mapped classes pass integrity and consistency.")
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

    _print_summary(coverage)
    _print_uncovered(coverage["uncovered"])
    return 0


def _print_summary(coverage: dict[str, Any]) -> None:
    """Print the headline coverage numbers, column-aligned and self-explanatory.

    The first block is all out of the same denominator (every test ID in the
    plan); ``released`` is a subset of ``implemented``. The trailing line is a
    separate count of *checks*, not plan items, and is called out as such to
    avoid mixing the two units.
    """
    total = coverage["plan_test_ids"]
    implemented = coverage["plan_test_ids_covered"]
    released = coverage["plan_test_ids_covered_by_released_class"]
    gap = len(coverage["uncovered"])
    checks = coverage["classes_with_test_ids"]

    def pct(n: int) -> str:
        return f"{round(100 * n / total)}%" if total else "-"

    rows = [
        ("Plan items (every test ID in the plan)", total, ""),
        ("Implemented (>=1 check maps to it)", implemented, pct(implemented)),
        ("...of which released (shipped to users)", released, pct(released)),
        ("Not yet implemented (the gap)", gap, pct(gap)),
    ]
    label_w = max(len(label) for label, _, _ in rows)

    print("Test-plan coverage  (source: docs/test-plan.yaml)\n")
    for label, value, percent in rows:
        print(f"  {label:<{label_w}}  {value:>4}  {percent:>4}")
    print(
        f"\n  ({checks} checks declare a test_id - a count of checks, not plan items; "
        "several\n   checks can map to the same plan item.)"
    )


def _print_uncovered(uncovered: list[dict[str, str]]) -> None:
    """Print the uncovered plan items as a compact table, ordered by milestone/priority."""
    if not uncovered:
        return
    print("\nUncovered plan items (no implementing check):\n")
    print(f"  {'Test ID':<13} {'Mile':<5} {'Pri':<4} {'Req':<8} Summary")
    print(f"  {'-' * 13} {'-' * 5} {'-' * 4} {'-' * 8} {'-' * 40}")
    for item in uncovered:
        summary = item["summary"] if len(item["summary"]) <= 70 else item["summary"][:67] + "..."
        print(f"  {item['test_id']:<13} {item['milestone']!s:<5} {item['priority']!s:<4} {item['req_id']:<8} {summary}")


if __name__ == "__main__":
    sys.exit(main())
