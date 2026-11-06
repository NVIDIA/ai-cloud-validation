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
"""A planned type that the operator chose not to build, with the operator's reason.

The scaffold's `selection.yaml`, beside the cases, lists each such type as
`not_selected` with `why` and `source`. No run will report the type, so
`unverified` would put it with the types that are built and not yet run. Its
evidence is the operator's reason, and `source` says who or what decided.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tier
import yaml

STATUS = "not_selected"
SOURCE = "source"
ASK = (
    "ask the operator why it is not built and who or what decided (an operator "
    "decision, a document, a release note), write the answer in `why` and `source` "
    "of its entry in selection.yaml beside the cases, and record again. Do not write "
    "a reason the operator did not give"
)


def skipped(cases: Path) -> dict[str, dict[str, Any]]:
    """Each `not_selected` entry of the selections in the cases directory, by type."""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(cases.rglob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("kind") != "selection":
            continue
        for entry in document.get("resources_skipped") or []:
            if isinstance(entry, dict) and entry.get("reason") == STATUS:
                found[str(entry.get("resource"))] = entry
    return found


def recorded(entry: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    """The entry of a skipped type: the status, and the reason only as the selection gives it.

    No run set the status, so the entry names no tier.
    """
    for name in (tier.FIELD, "evidence", SOURCE):
        entry.pop(name, None)
    entry["status"] = STATUS
    for field, given in (("evidence", selection.get("why")), (SOURCE, selection.get("source"))):
        if str(given or "").strip():
            entry[field] = str(given).strip()
    return entry


def cleared(entry: dict[str, Any]) -> dict[str, Any]:
    """A type that no selection skips any more keeps nothing of its skip."""
    if entry.get("status") == STATUS:
        for name in ("status", "evidence", SOURCE):
            entry.pop(name, None)
    return entry


def unstated(record: dict[str, Any]) -> list[str]:
    """Each `not_selected` resource with no reason or no source."""
    return [
        kind
        for kind, entry in sorted((record.get("resources") or {}).items())
        if isinstance(entry, dict)
        and entry.get("status") == STATUS
        and not (str(entry.get("evidence") or "").strip() and str(entry.get(SOURCE) or "").strip())
    ]


def gate(record: dict[str, Any], _: Any) -> list[str]:
    """A type the operator chose not to build states why, and who decided.

    A requirement needs it, so a report that shows the skip without the reason
    tells a reader that the requirement is not proved and not why.
    """
    return [f"{kind} is not_selected and states no reason; {ASK}" for kind in unstated(record)]


def section(resources: dict[str, Any], planned: dict[str, Any]) -> list[str]:
    """The report's list of each type not selected, what it leaves unproved, and why."""
    kinds = sorted(k for k, e in resources.items() if e.get("status") == STATUS and k in planned)
    if not kinds:
        return []
    lines = [
        "## Not built, by the operator's choice",
        "",
        "A requirement that needs one of these is not proved, and no run will prove",
        "it while the type is left out.",
        "",
        "| resource | leaves unproved | why | source |",
        "|---|---|---|---|",
    ]
    for kind in kinds:
        resource, entry = planned[kind], resources[kind]
        needs = ", ".join(f"`{name}`" for name in resource.requirements) or "dependency of " + ", ".join(
            f"`{name}`" for name in resource.dependency_of
        )
        lines.append(f"| `{kind}` | {needs} | {entry.get('evidence', '—')} | {entry.get(SOURCE, '—')} |")
    return [*lines, ""]
