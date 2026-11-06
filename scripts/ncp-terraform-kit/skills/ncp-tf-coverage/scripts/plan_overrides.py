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

"""A field fact that the operator states, where no condition can.

The documents are prose, and a fact that matters to the provider can sit where
no condition kind reaches it, or nowhere at all: a live run shows that an
update refuses a field. A `field_overrides` entry records the operator's
decision for one field of the object, with the reason: a change of it
`replaces` the object, changes it `in_place`, or the provider does not expose
it (`leave_out`). The provider applies the entry after its own derivation. The
gate checks only what the documents can check: that the field is the object's,
that a call could send it in place, that the create does not require a field
left out, and that no condition or update call already decides it.
"""

from __future__ import annotations

from typing import Any

import api_spec
import method_conditions
import operation_refs
import resource_shape
from api_spec import Spec

#: What the operator can decide about one field.
DECISIONS = ("replaces", "in_place", "leave_out")
KEYS = frozenset({"field", "decision", "reason"})


def overrides(resource: dict[str, Any]) -> list[dict[str, Any]]:
    """The resource's overrides, each one a mapping."""
    return [entry for entry in resource.get("field_overrides") or [] if isinstance(entry, dict)]


def failures(where: str, resource: dict[str, Any], spec: Spec | None) -> list[str]:
    """What breaks the resource's overrides."""
    entries = resource.get("field_overrides")
    if entries is None:
        return []
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return [f"{where}: `field_overrides` is a list of mappings of `field`, `decision` and `reason`"]
    found: list[str] = []
    seen: set[str] = set()
    for number, entry in enumerate(entries):
        at = f"{where}.field_overrides[{number}]"
        field = str(entry.get("field") or "")
        found += [f"{at}: `{key}` is not a key of an override" for key in sorted(set(entry) - KEYS)]
        if entry.get("decision") not in DECISIONS:
            found.append(f"{at}: decision {entry.get('decision')!r} is not one of {', '.join(DECISIONS)}")
        if not str(entry.get("reason") or "").strip():
            found.append(
                f"{at}: no `reason`. Give the operator's reason, such as the refusal that a live run "
                f"showed: a later step shows it to whoever reads the decision"
            )
        if field in seen:
            found.append(f"{at}: `{field}` has another override. One record decides a field")
        seen.add(field)
        if field and spec is not None:
            found += _field_failures(at, field, str(entry.get("decision") or ""), resource, spec)
    return found


def _field_failures(at: str, field: str, decision: str, resource: dict[str, Any], spec: Spec) -> list[str]:
    operations = resource.get("operations") or {}
    path = field.split(".")
    create = operation_refs.resolve(str(operations.get("create") or ""), spec)
    read = operation_refs.resolve(str(operations.get("read") or ""), spec)
    # Each body with the schemas of its own document, which its references name.
    roots = [
        (getattr(operation, "schemas", {}), getattr(operation, side, None))
        for operation, side in ((create, "request_body"), (read, "response_body"))
    ]
    # A read can wrap the object in an envelope such as `data`.
    top = api_spec.nested_fields(*roots[1], []) if roots[1][1] is not None else frozenset()
    candidates = [(root, path) for root in roots] + ([(roots[1], [*top, *path])] if len(top) == 1 else [])
    if not any(_node(root, steps) is not None for root, steps in candidates):
        return [
            f"{at}: `{field}` is not a field of the object, nor a path into one. Name the object's field "
            f"as its create or its read names it"
        ]
    found = _decided_failures(at, field, resource)
    # Only the provider step's derivation knows whether the update carries the
    # field at another path, so the message names each route with its condition.
    if decision == "in_place" and not _sent_in_place(path[0], operations, spec):
        found.append(
            f"{at}: `{field}` is `in_place`, but the resource's update does not take "
            f"`{path[0]}` in its request. If the update's request carries the field under another "
            f"name or path, record no override: the field stays left to your reading, and the "
            f"provider step records the request field that changes it. If another call sends the "
            f"field, plan that call as an `update_calls` entry. If no call can change the field, "
            f"decide `replaces`"
        )
    if decision == "leave_out" and path[-1] in _required(roots[0], path[:-1]):
        found.append(f"{at}: the create requires `{field}`, so a provider that leaves it out cannot create the object")
    return found


def _decided_failures(at: str, field: str, resource: dict[str, Any]) -> list[str]:
    """A `create_only` condition or an update call that already decides the
    field, the path to it, or a field inside it."""
    fixed = [
        str(f)
        for entry in method_conditions.conditions(resource)
        if entry.get("kind") == "create_only"
        for f in entry.get("fields") or []
    ]
    found = [
        f"{at}: a `create_only` condition already fixes `{other}`, so it decides `{field}`. One record "
        f"decides a field: correct that condition instead"
        for other in fixed
        if _related(field, other)
    ]
    found += [
        f"{at}: the update call {call.get('operation')} already changes `{other}`, so it decides "
        f"`{field}`. One record decides a field: correct that update call instead"
        for call in resource_shape.calls(resource)
        for other in (str(f) for f in call.get("fields") or [])
        if _related(field, other)
    ]
    return found


def _related(field: str, other: str) -> bool:
    return field == other or field.startswith(f"{other}.") or other.startswith(f"{field}.")


def _sent_in_place(top: str, operations: dict[str, Any], spec: Spec) -> bool:
    update = operation_refs.resolve(str(operations.get("update") or ""), spec)
    return update is not None and (top in update.request_fields or top in update.query_parameters)


def _resolved(schemas: dict[str, Any], node: Any) -> Any:
    """The schema that a `$ref` into `schemas` or an array's items stand for."""
    for _ in range(api_spec._REFERENCE_HOPS):
        if not isinstance(node, dict):
            return None
        if node.get("$ref"):
            node = schemas.get(str(node["$ref"]).rsplit("/", 1)[-1])
        elif node.get("type") == "array":
            node = node.get("items")
        else:
            return node
    return None


def _node(root: tuple[dict[str, Any], Any], path: list[str]) -> Any:
    """The schema at `path` inside the body of `root`, which pairs the
    schemas of the body's document with the body, or None."""
    schemas, node = root
    node = _resolved(schemas, node)
    for name in path:
        properties = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(properties, dict) or name not in properties:
            return None
        node = _resolved(schemas, properties[name])
    return node


def _required(root: tuple[dict[str, Any], Any], path: list[str]) -> frozenset[str]:
    node = _node(root, path)
    required = node.get("required") if isinstance(node, dict) else None
    return frozenset(str(name) for name in required) if isinstance(required, list) else frozenset()


def report_lines(planned: list[dict[str, Any]]) -> list[str]:
    """The decisions' group of each override, for the operator to approve."""
    rows = [
        f"| {resource.get('terraform_type')} | {entry.get('field')} | **{entry.get('decision')}** | "
        f"{method_conditions._cell(entry.get('reason'))} |"
        for resource in planned
        for entry in overrides(resource)
    ]
    if not rows:
        return []
    return [
        "### Your overrides",
        "",
        "You state each of these facts yourself: no condition of the documents states it. The provider "
        "applies it after its own derivation, and marks the field as your decision.",
        "",
        "| type | field | decision | reason |",
        "|---|---|---|---|",
        *rows,
        "",
    ]
