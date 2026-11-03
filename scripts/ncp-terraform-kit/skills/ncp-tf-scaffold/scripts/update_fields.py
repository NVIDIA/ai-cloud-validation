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

"""The fields a case's live update must change, derived from the plan.

A live update of one field proves that one field. A GCP image's `patch`
cannot change its labels, so a case that changes only the description leaves
the labels untested. A field that the plan updates in place only because
the update's body carries it is confirmed by no sentence, so a live update
that skips it leaves a wrong fact for a real user to find. The live update
therefore owes every field that the provider updates in place, by its path:
`in_place` reads them from the provider's derivation. A condition takes a
field out with its reason, and a field the case cannot give a value is listed
under `not_exercised` with its reason, so it stays visible.
"""

from __future__ import annotations

from typing import Any

import attribute_names

#: Why a condition takes a field out of the update, by its kind.
LEFT_OUT = {
    "needs_stopped": "its call needs a stopped object, and the plan's handling is refuse",
    "blocks_delete": "it blocks the delete that the case runs after the update",
    "create_only": "the documents say it cannot change after the create, so a change replaces the object",
}


def _resolved(reference: str, operations: dict[str, Any]) -> Any:
    from case_builder import resolve_operation

    return resolve_operation(reference, operations) if reference else None


def derive(
    resource: Any, operations: dict[str, Any] | None, in_place: tuple[tuple[str, str], ...] = ()
) -> dict[str, Any]:
    """`change`, `left_out` and `one_way` for one resource's update. `in_place`
    holds each field that the provider updates in place, as the document's
    path and the configuration's path."""
    operations = operations or {}
    if _resolved(resource.operations.get("create", ""), operations) is None:
        return {}
    update = _resolved(resource.operations.get("update", ""), operations)
    conditions = [
        (_resolved(ref, operations), kind, fields, handling, direction)
        for ref, kind, fields, handling, direction in resource.conditions
    ]
    from resource_shape import top_field

    by_call = {verb: {top_field(f) for f in fields} for verb, fields in resource.call_fields}
    owed = dict(in_place)
    left_out: dict[str, str] = {}
    for op, kind, fields, handling, _ in conditions:
        if kind == "needs_stopped" and handling == "refuse" and op is not None:
            verb = next((v for v, ref in resource.operations.items() if _resolved(ref, operations) == op), "")
            taken = by_call.get(verb, set()) if verb != "update" or op != update else set(fields or owed)
            left_out.update({path: LEFT_OUT[kind] for path in owed if top_field(path) in taken})
        elif kind == "blocks_delete":
            left_out.update({path: LEFT_OUT[kind] for path in owed if top_field(path) in set(fields)})
        elif kind == "create_only":
            left_out.update(dict.fromkeys(fields, LEFT_OUT[kind]))
    one_way = {
        owed[f]: direction
        for _, kind, fields, _, direction in conditions
        if kind == "one_way"
        for f in fields
        if f in owed and f not in left_out
    }
    found: dict[str, Any] = {
        "change": sorted(terraform for path, terraform in owed.items() if path not in left_out),
        "left_out": [
            {"field": owed.get(f) or attribute_names.configuration_path(f), "reason": reason}
            for f, reason in sorted(left_out.items())
        ],
    }
    if one_way:
        found["one_way"] = one_way
    return found


def refusals(case: dict[str, Any]) -> list[str]:
    """A filled update that does not change every field the plan can change,
    and a field left unexercised without its reason."""
    owed = (case.get("update_fields") or {}).get("change") or []
    update = (case.get("fixture") or {}).get("update")
    broken = [
        f"not_exercised names {entry.get('field') if isinstance(entry, dict) else entry!r} with no "
        f"reason. Say why the case cannot give it a changed value, or change it in fixture.update"
        for entry in case.get("not_exercised") or []
        if not isinstance(entry, dict) or not str(entry.get("reason") or "").strip()
    ]
    if not owed or not isinstance(update, dict) or not update:
        return broken
    excused = {
        str(entry.get("field"))
        for entry in case.get("not_exercised") or []
        if isinstance(entry, dict) and str(entry.get("reason") or "").strip()
    }
    missing = [field for field in owed if field not in excused and not _holds(update, field.split("."))]
    if missing:
        broken.append(
            f"fixture.update does not change {', '.join(missing)}. The plan updates each field in "
            f"update_fields.change in place; a live update of fewer fields leaves the rest untested. "
            f"List a field the case cannot give a changed value under not_exercised, with the reason"
        )
    return broken


def _holds(value: Any, path: list[str]) -> bool:
    """Whether the update sets the field at `path`; a list holds it when one item does."""
    if not path:
        return True
    if isinstance(value, list):
        return any(_holds(item, path) for item in value)
    return isinstance(value, dict) and path[0] in value and _holds(value[path[0]], path[1:])
