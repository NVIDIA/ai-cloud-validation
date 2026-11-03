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
"""The report's module table: one row for each layer that a module holds.

The rows come from the layer roll-up of the resources, which also has a group
for the types that no module holds. The caller leaves that group out, so a
reader never sees a module that does not exist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import tier


def section(
    rolled: dict[str, Any],
    modules: dict[str, Any],
    weakest: Callable[[list[str]], str],
) -> list[str]:
    """The "## Modules" section for the module layers in `rolled`."""
    lines = [
        "## Modules",
        "",
        "Two different facts, and a module needs both.",
        "",
        "**Its resources** must all be verified — a module cannot be sound if",
        "something it builds is unproven. **Its own test** must pass too: a module",
        "adds variables, outputs and wiring on top of its resources, so it can fail",
        "while every one of them passes. An output bound to the wrong attribute",
        "reaches no consumer, and no resource test would ever see it.",
        "",
        "So green resources are necessary and not sufficient, and this table does",
        "not merge the two columns.",
        "",
    ]
    if not rolled:
        return [*lines, "No module holds a type in this report.", ""]
    lines += [
        "| module | resources | its own test | status | held by | blocked by | evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, body in rolled.items():
        own = modules.get(name) or {}
        own_status = str(own.get("status") or "unverified")
        verdict = weakest([body["status"], own_status])
        held = ", ".join(f"`{k}`" for k in body["held_by"]) or "—"
        cited = ", ".join(f"`{i}`" for i in own.get("blocked_by") or []) or "—"
        lines.append(
            f"| `{name}` | {body['live']} / {body['resources']} | **{own_status}** | "
            f"**{verdict}** | {held} | {cited} | {own.get('evidence') or '—'} |"
        )
    lines.append("")
    # A failed module test also leaves `unverified`, and it names its tier.
    if all(
        str((modules.get(n) or {}).get("status")) == "unverified" and not (modules.get(n) or {}).get(tier.FIELD)
        for n in rolled
    ):
        lines += [
            "No module has been generated yet, so no module test has run. Every",
            "module is `unverified` for that reason and not because a test failed.",
            "",
        ]
    return lines
