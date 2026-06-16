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

"""Integrity checks for the requirements reconciliation layer.

Resolves the test <-> requirement graph against its sources of truth:

* ``docs/test-plan.yaml`` - the canonical test catalog (test side)
* ``docs/requirements/*-requirements.yaml`` - one structured listing per source
  (offtake, reference, and any future team docs), auto-discovered
* ``docs/requirements/test-requirements-matrix.yaml`` - the index joining them

``validate`` checks that **both** endpoints of every mapping edge resolve: each
``test_id`` exists in the test plan, and each ``req_id`` exists in the source
listing it claims (``source``). It fails (non-zero) on dangling edges, unknown
sources, duplicate/colliding ids, or an id used as both a test and a requirement.

``coverage`` reports, per source, the requirements that no test maps to (gaps).

Usage:
    python3 scripts/reqtrace.py validate
    python3 scripts/reqtrace.py coverage
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_PLAN = REPO_ROOT / "docs" / "test-plan.yaml"
REQ_DIR = REPO_ROOT / "docs" / "requirements"
MATRIX_DOC = REQ_DIR / "test-requirements-matrix.yaml"


def _load(path: Path) -> Any:
    """Parse a YAML file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_plan_test_ids(path: Path) -> set[str]:
    """Return the set of all ``test_id``s declared in the test plan."""
    data = _load(path)
    ids: set[str] = set()
    for domain in data.get("domains", []):
        for comp in domain.get("components", []):
            for cap in comp.get("capabilities", []):
                for test in cap.get("tests", []):
                    tid = test.get("test_id")
                    if tid:
                        ids.add(tid)
    return ids


def discover_source_files() -> list[tuple[str, Path]]:
    """Return ``(source_name, path)`` for every requirement listing, dups included.

    A source listing is any ``*.yaml`` in the requirements dir with top-level
    ``source`` and ``requirements`` keys (this excludes the mapping index).
    New team docs are picked up automatically. Unlike :func:`discover_sources`,
    this keeps every file so callers can detect source-name collisions.
    """
    out: list[tuple[str, Path]] = []
    for p in sorted(REQ_DIR.glob("*.yaml")):
        if p.name == MATRIX_DOC.name:
            continue
        data = _load(p)
        if isinstance(data, dict) and "source" in data and "requirements" in data:
            out.append((str(data["source"]), p))
    return out


def discover_sources() -> dict[str, Path]:
    """Map each requirement source name to its YAML listing.

    Convenience view over :func:`discover_source_files`. If two files declare
    the same name the last one wins here, so callers that care about integrity
    should also run :func:`duplicate_source_name_errors`.
    """
    return dict(discover_source_files())


def duplicate_source_name_errors(source_files: list[tuple[str, Path]]) -> list[str]:
    """Return an error per ``source`` name claimed by more than one listing.

    The ``source`` name is the primary key for its requirement listing; two
    files sharing one would silently shadow each other in
    :func:`discover_sources` (dict overwrite), dropping a whole listing with no
    signal. Fail loudly instead.
    """
    files_by_name: dict[str, list[str]] = {}
    for name, path in source_files:
        files_by_name.setdefault(name, []).append(path.name)
    return [
        f"source name {name!r} declared by multiple files: {', '.join(sorted(files))}"
        for name, files in sorted(files_by_name.items())
        if len(files) > 1
    ]


def source_req_ids(path: Path) -> tuple[list[str], list[str]]:
    """Return ``(req_ids, duplicates)`` from a source listing YAML."""
    seen: list[str] = []
    dupes: list[str] = []
    for r in _load(path).get("requirements", []):
        rid = r.get("req_id")
        if not rid:
            continue
        (dupes if rid in seen else seen).append(rid)
    return seen, dupes


def _mapping_edges(mapping: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield ``(test_id, req_id, source)`` for every edge in the index."""
    for m in mapping.get("mappings", []):
        tid = m.get("test_id", "")
        for req in m.get("requirements") or []:
            yield tid, req.get("req_id", ""), req.get("source", "")


def mapping_row_errors(
    mapping: dict[str, Any],
    plan_ids: set[str],
    source_sets: dict[str, set[str]],
) -> tuple[list[str], set[str], set[str]]:
    """Validate every mapping row and its requirement endpoints.

    Returns ``(errors, mapped_test_ids, mapped_reqs)``. A row only contributes to
    ``mapped_test_ids``/``mapped_reqs`` after its endpoints validate: an empty
    ``requirements`` list, or a requirement missing ``req_id``/``source``, is a
    loud error rather than a test that silently counts as "mapped" while having
    no resolvable requirement edge.
    """
    errors: list[str] = []
    mapped_test_ids: set[str] = set()
    mapped_reqs: set[str] = set()
    for m in mapping.get("mappings", []):
        tid = m.get("test_id", "")
        if not tid:
            errors.append("mapping: entry with empty test_id")
            continue
        if tid in mapped_test_ids:
            errors.append(f"mapping: duplicate test_id {tid!r}")
        mapped_test_ids.add(tid)
        if tid not in plan_ids:
            errors.append(f"mapping: test_id {tid!r} not in test-plan.yaml")

        reqs = m.get("requirements") or []
        if not reqs:
            errors.append(f"mapping: test {tid!r} has no requirements (empty edge)")
        for req in reqs:
            rid = req.get("req_id", "")
            src = req.get("source", "")
            if not rid or not src:
                errors.append(f"mapping: test {tid!r} has a requirement missing req_id/source")
                continue
            mapped_reqs.add(rid)
            if src not in source_sets:
                errors.append(f"mapping: test {tid!r} cites unknown source {src!r} for req {rid!r}")
            elif rid not in source_sets[src]:
                errors.append(f"mapping: req_id {rid!r} (test {tid!r}) not found in {src!r} listing")
    return errors, mapped_test_ids, mapped_reqs


def validate() -> int:
    """Resolve both endpoints of every mapping edge; return an exit code."""
    errors: list[str] = []
    warnings: list[str] = []

    plan_ids = load_plan_test_ids(TEST_PLAN)
    source_files = discover_source_files()
    if not source_files:
        errors.append("no source requirement listings found in docs/requirements/")
    errors.extend(duplicate_source_name_errors(source_files))
    sources = dict(source_files)

    # Per-source req ids, with within-source dup + cross-source ownership checks.
    source_sets: dict[str, set[str]] = {}
    owner: dict[str, str] = {}
    for sname, path in sources.items():
        ids, dupes = source_req_ids(path)
        source_sets[sname] = set(ids)
        for rid in dupes:
            errors.append(f"{sname}: duplicate req_id {rid!r}")
        for rid in set(ids):
            if rid in owner:
                errors.append(f"req_id {rid!r} defined in both {owner[rid]!r} and {sname!r} listings")
            else:
                owner[rid] = sname

    all_req_ids = set(owner)
    for x in sorted(all_req_ids & plan_ids):
        errors.append(f"id {x!r} is used as both a test_id and a req_id (must be globally unique)")

    mapping = _load(MATRIX_DOC)
    map_errors, mapped_test_ids, mapped_reqs = mapping_row_errors(mapping, plan_ids, source_sets)
    errors.extend(map_errors)

    for tid in sorted(plan_ids - mapped_test_ids):
        warnings.append(f"test {tid!r} has no mapping entry")

    if warnings:
        print(f"{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"\nvalidate FAILED with {len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
        return 1

    counts = ", ".join(f"{s}={len(source_sets[s])}" for s in sorted(source_sets))
    print(f"\nvalidate OK: {len(mapped_test_ids)} tests; sources [{counts}]; {len(mapped_reqs)} reqs mapped.")
    return 0


def coverage() -> int:
    """Report, per source, requirements that no test maps to."""
    sources = discover_sources()
    mapping = _load(MATRIX_DOC)
    mapped_reqs = {rid for _, rid, _ in _mapping_edges(mapping) if rid}

    for sname in sorted(sources):
        ids, _ = source_req_ids(sources[sname])
        gaps = [rid for rid in ids if rid not in mapped_reqs]
        print(f"{sname}: {len(ids) - len(gaps)}/{len(ids)} mapped to a test; {len(gaps)} with no test")
        for rid in gaps:
            print(f"  - {rid}")
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="cross-file integrity checks (both endpoints resolve)")
    sub.add_parser("coverage", help="per-source requirements with no mapped test")
    args = parser.parse_args()

    if args.command == "validate":
        sys.exit(validate())
    if args.command == "coverage":
        sys.exit(coverage())


if __name__ == "__main__":
    main()
