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

"""Render the test <-> requirement junction to an AsciiDoc table.

Joins ``docs/requirements/test-requirements-matrix.yaml`` with metadata from
``docs/test-plan.yaml`` and emits a **flat** traceability table (one row per
test/requirement pair). Flat-on-purpose: no row-span merging, so the result
pastes cleanly into Google Sheets.

Usage:
    python3 scripts/requirements_matrix_to_adoc.py [matrix.yaml]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_PLAN = REPO_ROOT / "docs" / "test-plan.yaml"
DEFAULT_MATRIX = REPO_ROOT / "docs" / "requirements" / "test-requirements-matrix.yaml"


def esc_adoc(val: Any) -> str:
    """Stringify `val` and escape the AsciiDoc cell delimiter; '' for None."""
    if val is None:
        return ""
    return str(val).replace("|", "\\|")


def cell(content: str) -> str:
    """Emit a regular AsciiDoc cell with no trailing whitespace when empty."""
    return f"| {content}" if content else "|"


def load_test_index(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map ``test_id`` -> useful test metadata from the test plan."""
    index: dict[str, dict[str, str]] = {}
    for domain in data.get("domains", []):
        for comp in domain.get("components", []):
            for cap in comp.get("capabilities", []):
                for test in cap.get("tests", []):
                    tid = test.get("test_id")
                    if not tid:
                        continue
                    index[tid] = {
                        "domain": domain.get("name", ""),
                        "component": comp.get("name", ""),
                        "summary": test.get("summary", ""),
                        "status": test.get("status", ""),
                    }
    return index


def generate_adoc(mapping: dict[str, Any], index: dict[str, dict[str, str]], outfile: Path) -> None:
    """Write the flat traceability AsciiDoc table to `outfile`."""
    lines = [
        "////",
        "GENERATED FILE - DO NOT EDIT BY HAND.",
        "Produced by scripts/requirements_matrix_to_adoc.py from",
        "docs/requirements/test-requirements-matrix.yaml. Run `make plan` to regenerate.",
        "////",
        "= Test \u2194 Requirement Traceability",
        ":toc:",
        ":icons: font",
        ":max-width: none",
        "",
        '[cols="2,2,5,2,2,1,3,3",options="header"]',
        "|===",
        "| Test ID | Test Domain | Function | Test Summary | Req ID | Source | Coverage | Notes",
        "",
    ]

    for m in mapping.get("mappings", []):
        tid = m.get("test_id", "")
        meta = index.get(tid, {})
        reqs = m.get("requirements") or [{}]
        notes = m.get("notes", "")
        for i, req in enumerate(reqs):
            # Anchor only on a test's first row; multi-req tests repeat the id
            # text on later rows (sheet-friendly) without re-declaring the anchor.
            if not tid:
                id_cell = ""
            elif i == 0:
                id_cell = f"[[{tid}]]{tid}"
            else:
                id_cell = tid
            parts = [
                cell(id_cell),
                cell(esc_adoc(meta.get("domain", ""))),
                cell(esc_adoc(meta.get("component", ""))),
                cell(esc_adoc(meta.get("summary", ""))),
                cell(esc_adoc(req.get("req_id", ""))),
                cell(esc_adoc(req.get("source", ""))),
                cell(esc_adoc(req.get("coverage", ""))),
                cell(esc_adoc(notes)),
            ]
            lines.append("\n".join(parts))
            lines.append("")

    lines.append("|===")
    outfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {outfile}")


def main() -> None:
    """Load the matrix + test plan and emit the traceability AsciiDoc."""
    matrix_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MATRIX

    with open(matrix_path, encoding="utf-8") as f:
        mapping = yaml.safe_load(f)
    with open(TEST_PLAN, encoding="utf-8") as f:
        plan = yaml.safe_load(f)

    index = load_test_index(plan)

    # Warn on any mapping that references a test_id absent from the plan.
    unknown = [m.get("test_id") for m in mapping.get("mappings", []) if m.get("test_id") not in index]
    if unknown:
        print(f"WARNING: {len(unknown)} mapping(s) reference unknown test_id(s): {unknown[:5]}...", file=sys.stderr)

    out = Path(re.sub(r"\.(yaml|yml)$", "", str(matrix_path)) + ".adoc")
    generate_adoc(mapping, index, out)


if __name__ == "__main__":
    main()
