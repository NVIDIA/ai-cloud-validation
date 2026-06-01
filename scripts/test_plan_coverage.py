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

Two modes:

* default - print a coverage report (optionally emit JSON/Markdown).
* ``--check`` - referential-integrity guardrail for CI: fail if any class
  declares a ``test_ids`` value that does not exist in docs/test-plan.yaml.

Offline: reads the committed test plan, the in-repo catalog, and the release
manifest. No network access required.

Usage:
    python3 scripts/test_plan_coverage.py            # report
    python3 scripts/test_plan_coverage.py --check    # CI guardrail
    python3 scripts/test_plan_coverage.py --markdown coverage.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = REPO_ROOT / "docs" / "test-plan.yaml"

MIN_REQ_LABEL = "min_req"


def load_plan(path: Path = PLAN_PATH) -> dict[str, dict[str, Any]]:
    """Return a mapping of ``test_id`` to its test-plan entry.

    Later duplicate ``test_id`` values overwrite earlier ones; duplicates are
    reported separately by :func:`integrity_errors`.
    """
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


def class_test_id_map() -> dict[str, list[str]]:
    """Return a mapping of validation class/variant name to declared test IDs.

    Includes unreleased classes so their metadata is validated too.
    """
    from isvtest.catalog import build_catalog

    return {
        entry["name"]: list(entry.get("test_ids", []))
        for entry in build_catalog(released_only=False)
        if entry.get("test_ids")
    }


def released_names() -> set[str]:
    """Return the set of released validation class names."""
    from isvtest.release_manifest import load_released_tests

    return load_released_tests()


def integrity_errors(plan_ids: set[str], class_map: dict[str, list[str]]) -> list[str]:
    """Return referential-integrity errors between class metadata and the plan.

    An error is raised when a class declares a ``test_ids`` value that does not
    exist in the test plan (typo or stale reference).
    """
    errors: list[str] = []
    for name in sorted(class_map):
        for tid in class_map[name]:
            if tid not in plan_ids:
                errors.append(f"{name}: declares unknown test_id {tid!r} (not in test-plan.yaml)")
    return errors


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

    def is_released(name: str) -> bool:
        return name in released or name.split("-")[0] in released

    min_req_ids = {tid for tid, e in plan_entries.items() if MIN_REQ_LABEL in (e.get("labels") or [])}

    def covered(tid: str, *, released_only: bool) -> bool:
        classes = test_id_to_classes.get(tid, [])
        return any(is_released(c) for c in classes) if released_only else bool(classes)

    covered_min_req = sorted(t for t in min_req_ids if covered(t, released_only=True))
    uncovered_min_req = sorted(t for t in min_req_ids if not covered(t, released_only=True))

    return {
        "plan_test_ids": len(plan_entries),
        "min_req_test_ids": len(min_req_ids),
        "min_req_covered_by_released_class": len(covered_min_req),
        "min_req_uncovered": uncovered_min_req,
        "classes_with_test_ids": len(class_map),
        "test_id_to_classes": {t: sorted(c) for t, c in sorted(test_id_to_classes.items())},
    }


def render_markdown(coverage: dict[str, Any], plan_entries: dict[str, dict[str, Any]]) -> str:
    """Render the coverage report as Markdown."""
    lines = [
        "# Test-plan coverage (via class `test_ids`)",
        "",
        f"- Test-plan items: **{coverage['plan_test_ids']}**",
        f"- `min_req` items: **{coverage['min_req_test_ids']}**",
        f"- `min_req` covered by a released class: **{coverage['min_req_covered_by_released_class']}**",
        f"- Validation classes declaring `test_ids`: **{coverage['classes_with_test_ids']}**",
        "",
        "## Covered test IDs",
        "",
        "| Test ID | Req | min_req | Implementing class(es) |",
        "|---|---|---|---|",
    ]
    for tid, classes in coverage["test_id_to_classes"].items():
        entry = plan_entries.get(tid, {})
        req = entry.get("req_id", "")
        mr = "yes" if MIN_REQ_LABEL in (entry.get("labels") or []) else ""
        lines.append(f"| `{tid}` | {req} | {mr} | {', '.join(f'`{c}`' for c in classes)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Referential-integrity guardrail: exit non-zero if a class references an unknown test_id.",
    )
    parser.add_argument("--json", metavar="PATH", help="Write the coverage report as JSON to PATH.")
    parser.add_argument("--markdown", metavar="PATH", help="Write the coverage report as Markdown to PATH.")
    args = parser.parse_args(argv)

    plan_entries = load_plan()
    plan_ids = set(plan_entries)
    class_map = class_test_id_map()

    errors = integrity_errors(plan_ids, class_map)
    if args.check:
        if errors:
            sys.stderr.write("test-plan coverage check failed:\n  " + "\n  ".join(errors) + "\n")
            return 1
        print(f"OK: {len(class_map)} classes reference only known test IDs.")
        return 0

    if errors:
        sys.stderr.write("WARNING: integrity errors found (run with --check in CI):\n  " + "\n  ".join(errors) + "\n")

    coverage = build_coverage(plan_entries, class_map, released_names())

    if args.json:
        Path(args.json).write_text(json.dumps(coverage, indent=2) + "\n")
        print(f"Wrote {args.json}")
    if args.markdown:
        Path(args.markdown).write_text(render_markdown(coverage, plan_entries))
        print(f"Wrote {args.markdown}")

    print(f"Test-plan items:                     {coverage['plan_test_ids']}")
    print(f"min_req items:                       {coverage['min_req_test_ids']}")
    print(f"min_req covered by released class:    {coverage['min_req_covered_by_released_class']}")
    print(f"Classes declaring test_ids:          {coverage['classes_with_test_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
