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

"""A requirement that names an object the documents create has a planned type.

`plan-completeness` asks of each cited creation whether a type claims it or an
unplanned entry excuses it. A requirement can still get nothing: its evidence
can name an object only by a field, or a type planned for another requirement
can claim the creation, or an entry can excuse the only object it needed. Its
row in the coverage report then reads the same as one that a type serves. So
each requirement, or part, that is `present` or `partial` and names a created
object needs a planned type that names it, in any layer, `other` included, or
`not_planned` with the reason, which the operator approves.
"""

from __future__ import annotations

from typing import Any

import operation_refs
from api_spec import Spec

KEY = "not_planned"
#: The statuses that say the documents serve the requirement, so something
#: can be built for it.
SERVED = frozenset({"present", "partial"})
TITLE = "Requirements and objects that this plan does not build"


def _units(mapping: dict[str, Any]) -> list[tuple[str, str | None, dict[str, Any]]]:
    """The requirement, or each of its parts: `(where, part_id, unit)`."""
    parts = [part for part in mapping.get("parts") or [] if isinstance(part, dict)]
    req_id = str(mapping.get("req_id"))
    if not parts:
        return [(req_id, None, mapping)]
    return [(f"{req_id}/{part.get('part_id')}", str(part.get("part_id")), part) for part in parts]


def _bound(module_plan: dict[str, Any]) -> set[tuple[str, str | None]]:
    """Each `(req_id, part_id)` that a planned type names."""
    found: set[tuple[str, str | None]] = set()
    for layer in (module_plan.get("layers") or {}).values():
        for resource in (layer or {}).get("resources") or []:
            for item in (resource or {}).get("requirements") or []:
                if isinstance(item, dict):
                    found.add((str(item.get("req_id")), item.get("part_id")))
                else:
                    found.add((str(item), None))
    return found


def _creation_of_item(operation: Any, spec: Spec) -> bool:
    """Whether an operation acts on an item of a collection the documents create."""
    identifier = operation.operation_id
    if "." in identifier:
        prefix = identifier.rsplit(".", 1)[0]
        return any(f"{prefix}.{verb}" in spec.operations for verb in operation_refs.CREATING_VERBS)
    segments = operation.normalized_path.strip("/").split("/")
    if segments and segments[-1] != operation_refs.PARAMETER:
        segments = segments[:-1]
    if len(segments) < 2 or segments[-1] != operation_refs.PARAMETER:
        return False
    collection = "/" + "/".join(segments[:-1])
    creation = operation_refs.resolve(f"POST {collection}", spec)
    return creation is not None and operation_refs.creates(creation, spec)


def names_created_object(reference: str, spec: Spec, bodies: frozenset[str]) -> bool:
    """Whether a reference names an object that a call of the documents creates:
    the creation itself, a call on one of its items, or a field of its body."""
    if reference.startswith(operation_refs.SCHEMA_PREFIX):
        return reference[len(operation_refs.SCHEMA_PREFIX) :].split(".", 1)[0] in bodies
    operation = operation_refs.resolve(reference, spec)
    if operation is None:
        return False
    return operation_refs.creates(operation, spec) or _creation_of_item(operation, spec)


def failures(coverage: dict[str, Any], module_plan: dict[str, Any], spec: Spec) -> list[str]:
    bound = _bound(module_plan)
    bodies = frozenset(
        operation.request_schema
        for operation in spec.operations.values()
        if operation.request_schema and operation_refs.creates(operation, spec)
    )
    found: list[str] = []
    for mapping in coverage.get("mappings") or []:
        req_id = str(mapping.get("req_id"))
        for where, part_id, unit in _units(mapping):
            served = (req_id, part_id) in bound or (part_id is None and any(r == req_id for r, _ in bound))
            reason = unit.get(KEY)
            if reason is not None and served:
                found.append(f"{where}: a planned type names it, so it takes no `{KEY}`")
            elif reason is not None and not str(reason).strip():
                found.append(f"{where}: `{KEY}` states the reason that no planned type serves it")
            if served or reason is not None or unit.get("status") not in SERVED:
                continue
            named = [ref for ref in unit.get("evidence") or [] if names_created_object(str(ref), spec, bodies)]
            if named:
                found.append(
                    f"{where}: its evidence {', '.join(named)} names an object that the documents create, and "
                    f"no planned type names it. Plan the type, in `other` when no layer fits it, and name "
                    f"the requirement under its `requirements`, or state `{KEY}` with the reason for the "
                    f"operator"
                )
    return found


def _cell(text: Any) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def report_lines(coverage: dict[str, Any], module_plan: dict[str, Any]) -> list[str]:
    """The requirements that no planned type serves, with the reason, and the
    creations that the plan leaves out, with theirs."""
    reasons = [
        (where, unit, mapping)
        for mapping in coverage.get("mappings") or []
        for where, _, unit in _units(mapping)
        if unit.get(KEY)
    ]
    unplanned = [entry for entry in module_plan.get("unplanned_operations") or [] if isinstance(entry, dict)]
    if not reasons and not unplanned:
        return []
    lines = [
        f"### {TITLE}",
        "",
        "No provider resource, module or test builds these. Approve each reason, or ask for the type.",
        "",
    ]
    if reasons:
        lines += ["| requirement | status | layers | why no planned type |", "|---|---|---|---|"]
        lines += [
            f"| {where} | {unit.get('status', mapping.get('status', ''))} | "
            f"{', '.join(unit.get('layers') or [unit.get('layer') or ''])} | {_cell(unit.get(KEY))} |"
            for where, unit, mapping in reasons
        ]
        lines.append("")
    if unplanned:
        lines += ["| creation | why it is not planned |", "|---|---|"]
        lines += [f"| {entry.get('operation')} | {_cell(entry.get('reason'))} |" for entry in unplanned]
        lines.append("")
    return lines[:-1]
