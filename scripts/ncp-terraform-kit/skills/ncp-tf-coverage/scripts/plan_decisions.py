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

"""The choices the plan makes for the operator, first in the report.

A plan picks how the provider handles a call on a stopped object, what
terraform destroy does with an object that the API never deletes, and which
methods it leaves out. Each one costs downtime, leaves something in the
account, or drops a capability, so
the operator approves it. The report gives the details in its later sections,
and a review of the whole report missed them, so they come first.
"""

from __future__ import annotations

from typing import Any

import destroy_handling
import method_conditions
import plan_overrides
from replaced_types import Replaced

#: What each handling does, as the operator approves it.
MEANING = {
    ("needs_stopped", "refuse"): "The provider reads the object first. It sends the call when the object "
    "is stopped, and terraform apply fails on a change through it while the "
    "object runs.",
    ("needs_stopped", "stop_and_start"): "The provider stops the object, waits until it is stopped, makes the "
    "change, and starts it again: downtime on each such change.",
}


#: The condition each handling answers, in the words of the decision.
KIND_WORDS = {
    "needs_stopped": "a call that needs the object stopped",
}


def _handlings(planned: list[dict[str, Any]]) -> list[str]:
    """One group for each handling: the handling in bold, what it does, the
    other choice, and the rows it applies to."""
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for resource in planned:
        for entry in method_conditions.conditions(resource):
            if entry.get("handling"):
                groups.setdefault((str(entry.get("kind")), str(entry.get("handling"))), []).append((resource, entry))
    lines: list[str] = []
    for (kind, handling), entries in groups.items():
        lines += [
            f"### **{handling}** for {KIND_WORDS.get(kind, kind)} (`{kind}`)",
            "",
            MEANING.get((kind, handling), ""),
            "",
        ]
        others = [other for other in method_conditions.HANDLING.get(kind, ()) if other != handling]
        lines += [f"The other choice is **{other}**. {MEANING.get((kind, other), '')}" for other in others]
        stopped = kind == "needs_stopped"
        lines += [
            "",
            "| type | method | " + ("stopped when | " if stopped else "") + "quote |",
            "|---|---|" + ("---|" if stopped else "") + "---|",
        ]
        lines += [
            f"| {resource.get('terraform_type')} | {entry.get('operation')} | "
            + (f"{_stopped(entry)} | " if stopped else "")
            + f"{method_conditions._cell(entry.get('quote'))} |"
            for resource, entry in entries
        ]
        lines.append("")
    return lines[:-1]


def _stopped(entry: dict[str, Any]) -> str:
    """The state that the provider reads as stopped."""
    stopped = entry.get("stopped_when")
    if isinstance(stopped, dict) and stopped.get("field"):
        return f"`{stopped.get('field')}` is `{stopped.get('value')}`"
    return ""


def _refused_replacements(planned: list[dict[str, Any]], replaced: list[Replaced]) -> list[str]:
    """A replacement destroys the object before it creates it again, so a type
    that refuses its delete fails each change that replaces it."""
    refused = destroy_handling.refused(planned)
    return [
        f"- {r.terraform_type} refuses its delete, so a change of {_changes(r)} fails at apply: "
        f"the replacement needs the delete."
        for r in replaced
        if r.terraform_type in refused
    ]


def _changes(replaced: Replaced) -> str:
    if replaced.every:
        return "any settable field"
    return ", ".join([*replaced.fields, *(f"{name} on a {way}" for name, way in replaced.one_way)])


def _unmanaged(planned: list[dict[str, Any]], plan: dict[str, Any]) -> list[str]:
    count = sum(len(resource.get("unmanaged_methods") or []) for resource in planned)
    count += len(plan.get("unmanaged_methods") or [])
    if not count:
        return []
    return [
        f"- {count} method(s) are not managed, each with a reason. A method that is left out is a "
        f"capability that the provider does not have: see Methods the plan does not manage."
    ]


def _replaced(replaced: list[Replaced]) -> list[str]:
    kinds = [r.terraform_type for r in replaced]
    if not kinds:
        return []
    return [
        f"- {len(kinds)} type(s) are destroyed and created again on some changes, which can lose "
        f"data: {', '.join(kinds)}. See Changes that replace."
    ]


def report_lines(
    planned: list[dict[str, Any]],
    plan: dict[str, Any],
    replaced: list[Replaced],
    unbuilt: list[str],
    stated: list[str] | None = None,
) -> list[str]:
    """The module plan's first section: each choice that the operator approves.
    `unbuilt` lists what the plan does not build, with each reason. `stated`
    holds each replacement that an unmanaged method of the object may avoid."""
    replaced = [r for r in replaced if r.replaces]
    lines = _replaced(replaced) + _refused_replacements(planned, replaced) + _unmanaged(planned, plan)
    handlings = _handlings(planned)
    if destroyed := destroy_handling.report_lines(planned):
        handlings = [*handlings, "", *destroyed] if handlings else destroyed
    lines = [*lines, "", *handlings] if lines else handlings
    if stated:
        lines = [*lines, "", *stated] if lines else stated
    if chosen := plan_overrides.report_lines(planned):
        lines = [*lines, "", *chosen] if lines else chosen
    if unbuilt:
        lines = [*lines, "", *unbuilt] if lines else unbuilt
    if not lines:
        return []
    return [
        "## Decisions to approve",
        "",
        "The plan makes these choices for you, and you own each one: a wrong one costs money, "
        "downtime or data. Check each one against what you need. Approve it with the plan, or say "
        "what to change.",
        "",
        *lines,
        "",
    ]
