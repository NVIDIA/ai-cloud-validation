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

"""Renderers for doctor reports.

Two output modes:

- ``render_rich``: grouped, colored, human-readable summary for terminals.
- ``render_json``: stable JSON contract for CI consumers.
"""

import json
from typing import Any

from rich.console import Console

from isvctl.doctor.result import CategoryReport, CheckResult, Status, worst

_GLYPHS: dict[Status, str] = {
    Status.OK: "[✓]",
    Status.WARN: "[⚠]",
    Status.FAIL: "[✗]",
    Status.SKIP: "[-]",
}

_STYLES: dict[Status, str] = {
    Status.OK: "green",
    Status.WARN: "yellow",
    Status.FAIL: "red",
    Status.SKIP: "dim",
}


def _glyph(status: Status) -> str:
    """Render a status glyph with Rich markup."""
    return f"[{_STYLES[status]}]{_GLYPHS[status]}[/{_STYLES[status]}]"


def _aggregate_counts(reports: list[CategoryReport]) -> dict[Status, int]:
    """Sum per-status counts across all reports (single source of truth)."""
    counts: dict[Status, int] = {s: 0 for s in Status}
    for report in reports:
        for status, n in report.counts().items():
            counts[status] += n
    return counts


def render_rich(
    reports: list[CategoryReport],
    *,
    isvctl_version: str,
    verbose: bool = False,
    console: Console | None = None,
) -> None:
    """Print a grouped, colored summary to the console."""
    out = console or Console()
    overall = worst([r.worst_status for r in reports])

    out.print(
        f"[bold]isvctl doctor[/bold] {isvctl_version}  —  "
        f"{len(reports)} categor{'y' if len(reports) == 1 else 'ies'} checked"
    )
    out.print()

    for report in reports:
        header = f"{_glyph(report.worst_status)} [bold]{report.name}[/bold]"
        out.print(header)

        # Group results by their `group` field, preserving insertion order so
        # the env category prints "ISV Lab Service" before "NGC" etc.
        groups: dict[str | None, list[CheckResult]] = {}
        for r in report.results:
            groups.setdefault(r.group, []).append(r)

        for group_label, items in groups.items():
            if group_label:
                out.print(f"    [dim]{group_label}[/dim]")
                indent = "      "
            else:
                indent = "    "
            for r in items:
                line = f"{indent}{_glyph(r.status)} {r.name}"
                if r.message:
                    line += f"  {r.message}"
                out.print(line)
                if r.remediation and r.status in (Status.FAIL, Status.WARN):
                    out.print(f"{indent}    [dim]hint:[/dim] {r.remediation}")
                if verbose and r.detail:
                    for detail_line in r.detail.splitlines():
                        out.print(f"{indent}    [dim]{detail_line}[/dim]")
        out.print()

    counts = _aggregate_counts(reports)

    summary_bits = (
        f"[green]{counts[Status.OK]} ok[/green]",
        f"[yellow]{counts[Status.WARN]} warn[/yellow]",
        f"[red]{counts[Status.FAIL]} fail[/red]",
        f"[dim]{counts[Status.SKIP]} skip[/dim]",
    )
    out.print("Summary: " + ", ".join(summary_bits))

    if overall == Status.FAIL:
        out.print("[red]Doctor found issues that will block isvctl runs.[/red]")
    elif overall == Status.WARN:
        out.print("[yellow]Doctor found warnings; review hints above.[/yellow]")
    else:
        out.print("[green]All checks passed.[/green]")


def render_json(
    reports: list[CategoryReport],
    *,
    isvctl_version: str,
    verbose: bool = False,
) -> str:
    """Return a stable JSON payload for the report.

    ``detail`` (tool paths, versions) is only emitted when ``verbose`` is set,
    mirroring ``render_rich`` so a plain ``--json`` run does not leak local
    filesystem paths; the key is always present (``null`` when withheld).

    Shape (stable contract — do not break without versioning):

        {
          "isvctl_version": "<version>",
          "overall_status": "OK|WARN|FAIL|SKIP",
          "categories": [{
            "name": "<category>",
            "status": "<status>",
            "results": [{
              "name": "...", "status": "...", "message": "...",
              "group": "..." | null, "remediation": "..." | null,
              "detail": "..." | null
            }],
          }, ...],
          "summary": {"ok": N, "warn": N, "fail": N, "skip": N}
        }
    """
    counts = _aggregate_counts(reports)
    categories: list[dict[str, Any]] = []
    for report in reports:
        cat_results: list[dict[str, Any]] = []
        for r in report.results:
            cat_results.append(
                {
                    "name": r.name,
                    "status": r.status.value,
                    "message": r.message,
                    "group": r.group,
                    "remediation": r.remediation,
                    "detail": r.detail if verbose else None,
                }
            )
        categories.append(
            {
                "name": report.name,
                "status": report.worst_status.value,
                "results": cat_results,
            }
        )

    payload = {
        "isvctl_version": isvctl_version,
        "overall_status": worst([r.worst_status for r in reports]).value,
        "categories": categories,
        "summary": {
            "ok": counts[Status.OK],
            "warn": counts[Status.WARN],
            "fail": counts[Status.FAIL],
            "skip": counts[Status.SKIP],
        },
    }
    return json.dumps(payload, indent=2)
