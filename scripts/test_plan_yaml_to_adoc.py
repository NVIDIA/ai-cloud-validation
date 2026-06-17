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

"""
Convert test-plan.yaml into an AsciiDoc table with row-spanned merged cells.

Deep linking:
  Each test gets an inline anchor ``[[<test_id>]]`` in the Test ID cell,
  which asciidoctor renders as ``<a id="<test_id>">``. Link to it as
  ``test-plan.html#<test_id>`` once rendered.

Usage:
    python3 test_plan_yaml_to_adoc.py [input.yaml]
"""

import re
import sys
from pathlib import Path
from typing import Any

import yaml

GH_REPO = "NVIDIA/ai-cloud-validation"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Constraint shared by `labels` and `dependencies` entries: identifier-like only.
LABEL_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
ISSUE_REF_RE = re.compile(r"#(\d+)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def count_tests(node: dict[str, Any], level: str) -> int:
    """Recursively count the tests beneath a domain/component/capability node."""
    if level == "domain":
        return sum(count_tests(c, "component") for c in node.get("components", []))
    if level == "component":
        return sum(count_tests(c, "capability") for c in node.get("capabilities", []))
    return len(node.get("tests", []))


def esc_adoc(val: Any) -> str:
    """Stringify `val` and escape the AsciiDoc cell delimiter; empty string for None."""
    if val is None:
        return ""
    return str(val).replace("|", "\\|")


def fmt_list(values: list[str] | None) -> str:
    """Render a list of strings as a comma-separated, AsciiDoc-escaped cell value."""
    if not values:
        return ""
    return ", ".join(esc_adoc(v) for v in values)


def cell(content: str) -> str:
    """Emit a regular AsciiDoc table cell, with no trailing whitespace when empty."""
    return f"| {content}" if content else "|"


def acell(content: str) -> str:
    """Emit an AsciiDoc raw (``a|``) cell, with no trailing whitespace when empty."""
    return f"a| {content}" if content else "a|"


def span_cell(span: int, content: str) -> str:
    """Emit a row-spanning cell, with no trailing whitespace when content is empty."""
    base = f".{span}+|" if span > 1 else "|"
    return f"{base} {content}" if content else base


def fmt_gh_issues_adoc(entries: list[str] | None) -> str:
    """Render bare ``#N`` GitHub issue entries as linked AsciiDoc."""
    if not entries:
        return ""
    parts = []
    for entry in entries:
        m = ISSUE_REF_RE.fullmatch(str(entry))
        if not m:
            parts.append(esc_adoc(entry))
            continue
        num = m.group(1)
        link = f"https://github.com/{GH_REPO}/issues/{num}"
        parts.append(f"{link}[#{num}]")
    return " +\n".join(parts)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_string_list(field_name: str, values: Any, test_id: str, errors: list[str]) -> None:
    """Append errors for any entry in `values` that is not an identifier-like string."""
    if values is None:
        return
    if not isinstance(values, list):
        errors.append(f"{test_id}: {field_name!r} must be a list, got {type(values).__name__}")
        return
    for v in values:
        if not isinstance(v, str) or not LABEL_RE.match(v):
            errors.append(f"{test_id}: invalid {field_name} entry {v!r}")


def validate_test_plan(data: dict[str, Any]) -> None:
    """Walk every test and verify `labels` and `dependencies` are well-formed.

    Raises ``SystemExit`` with a list of all problems found, so a single run
    surfaces every issue instead of failing on the first.
    """
    errors = []
    for d in data.get("domains", []):
        for comp in d.get("components", []):
            for cap in comp.get("capabilities", []):
                for t in cap.get("tests", []):
                    tid = t.get("test_id", "<no test_id>")
                    _validate_string_list("labels", t.get("labels"), tid, errors)
                    _validate_string_list("dependencies", t.get("dependencies"), tid, errors)
                    for entry in t.get("github_issues") or []:
                        if not re.fullmatch(r"#\d+", str(entry)):
                            errors.append(f"{tid}: github_issues entry {entry!r} must be a bare '#N' reference")
    if errors:
        raise SystemExit("test-plan.yaml validation failed:\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------------------
# AsciiDoc generation
# ---------------------------------------------------------------------------


def generate_adoc(data: dict[str, Any], outfile: str) -> None:
    """Write the test-plan AsciiDoc table for `data` to `outfile`."""
    lines = [
        "////",
        "GENERATED FILE - DO NOT EDIT BY HAND.",
        "Produced by scripts/test_plan_yaml_to_adoc.py from docs/test-plan.yaml.",
        "Run `make plan` to regenerate.",
        "////",
        "= AI Cloud Validation Test Suite",
        ":toc:",
        ":max-width: none",
        "",
        '[cols="2,2,5,2,2,1,1,1,1,1,3,1,1,1,6,1,4",options="header"]',
        "|===",
        "| Test Domain | Function | Description | Example | Test ID | Req ID | GH | Labels | Notes | Priority | Issues | Dependencies | Milestone | Release | Test Cases | Status | Justification",
        "",
    ]

    for domain in data.get("domains", []):
        d_span = count_tests(domain, "domain")
        if d_span == 0:
            continue
        first_d = True
        for comp in domain.get("components", []):
            f_span = count_tests(comp, "component")
            if f_span == 0:
                continue
            first_f = True
            for cap in comp.get("capabilities", []):
                tests = cap.get("tests", [])
                cap_span = len(tests)
                if cap_span == 0:
                    continue
                first_cap = True
                for test in tests:
                    parts = []
                    if first_d:
                        parts.append(span_cell(d_span, esc_adoc(domain["name"])))
                        first_d = False
                    if first_f:
                        parts.append(span_cell(f_span, esc_adoc(comp["name"])))
                        first_f = False
                    if first_cap:
                        parts.append(span_cell(cap_span, esc_adoc(cap.get("description", ""))))
                        parts.append(span_cell(cap_span, esc_adoc(cap.get("example", ""))))
                        first_cap = False
                    test_id = test.get("test_id", "")
                    parts.append(cell(f"[[{test_id}]]{test_id}") if test_id else cell(""))
                    parts.append(cell(esc_adoc(test.get("req_id", ""))))
                    gh_entries = test.get("github_issues", [])
                    first_gh_link = ""
                    if gh_entries:
                        m = ISSUE_REF_RE.fullmatch(str(gh_entries[0]))
                        if m:
                            num = m.group(1)
                            first_gh_link = f"https://github.com/{GH_REPO}/issues/{num}[#{num}]"
                    parts.append(cell(first_gh_link))
                    parts.append(cell(fmt_list(test.get("labels", []))))
                    parts.append(cell(esc_adoc(test.get("notes", ""))))
                    parts.append(cell(esc_adoc(test.get("priority", ""))))
                    parts.append(acell(fmt_gh_issues_adoc(test.get("github_issues", []))))
                    parts.append(cell(fmt_list(test.get("dependencies", []))))
                    parts.append(cell(esc_adoc(test.get("milestone", ""))))
                    parts.append(cell(esc_adoc(test.get("release", ""))))
                    parts.append(cell(esc_adoc(test.get("summary", ""))))
                    parts.append(cell(esc_adoc(test.get("status", ""))))
                    parts.append(cell(esc_adoc(test.get("justification", ""))))
                    lines.append("\n".join(parts))
                    lines.append("")

    lines.append("|===")
    with open(outfile, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {outfile}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Load the test-plan YAML (CLI arg or default), validate it, and emit the AsciiDoc."""
    infile = str(REPO_ROOT / "docs" / "test-plan.yaml")
    if len(sys.argv) > 1:
        infile = sys.argv[1]

    with open(infile, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    validate_test_plan(data)

    base = re.sub(r"\.(yaml|yml)$", "", infile)
    generate_adoc(data, f"{base}.adoc")


if __name__ == "__main__":
    main()
