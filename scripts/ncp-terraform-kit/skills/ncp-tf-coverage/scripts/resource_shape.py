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

"""The keys a planned resource may hold, and the calls that change its object.

An object can change through more than its update: GCP's `images.patch`
changes only "family, description, deprecation status", and the labels change
through `setLabels`. Each such call goes under `update_calls` with the fields
of the object that it changes, so the provider sends a change of those fields
through it. The provider's attributes are the object's fields, so a field
of the call's body that the object does not hold, such as the `items` of
`setMetadata`, reaches no attribute. A key that the shape does not define
reads as a record and is read by nothing, so it is refused.
"""

from __future__ import annotations

import json
import re
from typing import Any

import api_spec
import operation_refs
from api_spec import Spec

#: The lifecycle verbs under `operations`.
VERBS = ("create", "read", "update", "delete")

#: Every key a planned resource may hold.
KEYS = frozenset(
    {
        "logical_kind",
        "terraform_type",
        "operations",
        "requirements",
        "dependency_of",
        "note",
        "scope_alternatives",
        "scope_note",
        "update_note",
        "entry_methods",
        "entries_note",
        "update_calls",
        "unmanaged_methods",
        "conditions",
        "field_overrides",
        "on_destroy",
        "documents_silent",
    }
)

#: The keys of one `update_calls` entry. `value` is the value of its field
#: that the call sets, which only calls that share a field need.
#: `carried_as` names, for a field that the request takes under another name,
#: the request field or query parameter that carries it.
CALL_KEYS = frozenset({"operation", "fields", "value", "carried_as"})

_WORD_START = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def call_verb(reference: str) -> str:
    """An update call's verb: its method's name in snake case, read from the
    reference alone, so every skill derives the same verb.

    `op:compute.images.setLabels` gives `set_labels`, and
    `POST /v1/instances/{id}/changeType` gives `change_type`.
    """
    text = reference.removeprefix("op:").strip()
    if " " in text:
        name = re.split(r"[/:]", text.split(" ", 1)[1])[-1]
    else:
        name = text.rsplit(".", 1)[-1]
    return _WORD_START.sub("_", name).lower()


def calls(resource: dict[str, Any]) -> list[dict[str, Any]]:
    """The resource's update calls, each one a mapping."""
    return [call for call in resource.get("update_calls") or [] if isinstance(call, dict)]


def verbs(resource: dict[str, Any]) -> dict[str, str]:
    """Every call that the provider sends for the resource, by verb: its
    operations, then each update call under its own verb."""
    found = {str(verb): str(ref) for verb, ref in (resource.get("operations") or {}).items()}
    for call in calls(resource):
        reference = str(call.get("operation") or "")
        found[call_verb(reference)] = reference
    return found


def call_fields(resource: dict[str, Any]) -> dict[str, list[str]]:
    """The object's fields that each update call changes, by verb. A field can
    be a path into one, such as `metadata.items`."""
    return {
        call_verb(str(call.get("operation") or "")): [str(f) for f in call.get("fields") or []]
        for call in calls(resource)
    }


def call_values(resource: dict[str, Any]) -> dict[str, Any]:
    """The value of its field that each update call sets, by verb, for each
    call that states one."""
    return {call_verb(str(call.get("operation") or "")): call["value"] for call in calls(resource) if "value" in call}


def report_lines(planned: list[dict[str, Any]], spec: Spec | None) -> list[str]:
    """The report's table of each update call, the fields it changes, and the
    fields its request carries."""
    rows = [
        f"| {resource.get('terraform_type')} | {call.get('operation')} | "
        f"{', '.join(_named_field(call, str(f)) for f in call.get('fields') or [])} | "
        f"{_carried(str(call.get('operation') or ''), spec)} | "
        f"{json.dumps(call['value']) if 'value' in call else ''} |"
        for resource in planned
        for call in calls(resource)
    ]
    if not rows:
        return []
    return [
        "",
        "## Update calls",
        "",
        "Each call besides the update that changes the object, the fields of the object that it "
        "changes, and the fields that its request carries. The provider sends a change of these "
        "fields through that call alone.",
        "",
        "| type | method | fields | request carries | value |",
        "|---|---|---|---|---|",
        *rows,
    ]


def _named_field(call: dict[str, Any], field: str) -> str:
    """The field, with the request field that carries it under another name."""
    renamed = (call.get("carried_as") or {}).get(field) if isinstance(call.get("carried_as"), dict) else None
    if not renamed:
        return field
    return f"{field} (as {', '.join([renamed] if isinstance(renamed, str) else [str(n) for n in renamed])})"


def _carried(reference: str, spec: Spec | None) -> str:
    """The fields of the call's request body and its query parameters. The
    plan names the object's field, and only the request shows which value the
    call sends."""
    operation = operation_refs.resolve(reference, spec) if spec is not None else None
    if operation is None:
        return "?"
    carried = [*operation.request_fields, *(f"{name} (query)" for name in operation.query_parameters)]
    return ", ".join(carried) or "nothing"


def top_field(field: str) -> str:
    """The object's field that a path names: `metadata.items` is `metadata`."""
    return str(field).split(".", 1)[0]


def failures(where: str, resource: dict[str, Any], spec: Spec | None) -> list[str]:
    """What breaks the shape of one planned resource."""
    found = [
        f"{where}: `{key}` is not a key of a planned resource. Record a call that "
        f"changes the object under `update_calls`, and anything else in `note`"
        for key in sorted(set(resource) - KEYS)
    ]
    operations = resource.get("operations") or {}
    found += [
        f"{where}: `operations.{verb}` is not one of {', '.join(VERBS)}. Record a call "
        f"that changes the object under `update_calls`, with the fields it changes"
        for verb in sorted(set(operations) - set(VERBS))
    ]
    entries = resource.get("update_calls")
    if entries is None:
        return found
    if not isinstance(entries, list):
        return [*found, f"{where}: `update_calls` is not a list"]
    planned = {operation_refs.identity(str(ref), spec) for ref in operations.values()} if spec else set()
    held = object_fields(operations, spec)
    seen: set[str] = set()
    for number, call in enumerate(entries):
        found += _call_failures(f"{where}.update_calls[{number}]", call, planned, seen, spec)
        found += _field_failures(f"{where}.update_calls[{number}]", call, held)
        found += _carry_failures(f"{where}.update_calls[{number}]", call, operations, held, spec)
    return found + _shared_field_failures(where, entries)


def _carry_failures(
    where: str, call: Any, operations: dict[str, Any], held: frozenset[str], spec: Spec | None
) -> list[str]:
    """A field that the call's request does not carry. The provider sends a
    change of the field through the call, so a call mapped to a field that
    its request does not hold sends some other value. A request carries a
    field by its name, by the field's own schema as its body, or under the
    name that `carried_as` gives; a call that states its `value` carries none."""
    if spec is None or not isinstance(call, dict) or "value" in call or not isinstance(call.get("fields"), list):
        return []
    operation = operation_refs.resolve(str(call.get("operation") or ""), spec)
    if operation is None:
        return []
    carried = set(operation.request_fields) | set(operation.query_parameters)
    renamed = call.get("carried_as") or {}
    if not isinstance(renamed, dict):
        return [f"{where}: `carried_as` maps each field of the object to the request field that carries it"]
    found = [
        f"{where}: `carried_as` names `{name}`, which {call.get('operation')} does not take: its request "
        f"carries {', '.join(sorted(carried)) or 'nothing'}"
        for value in renamed.values()
        for name in ([value] if isinstance(value, str) else value or [])
        if name not in carried
    ]
    found += [
        f"{where}: `carried_as` names `{field}`, which is not one of the call's `fields`"
        for field in renamed
        if field not in call["fields"]
    ]
    for field in (str(f) for f in call["fields"]):
        if field in renamed or _carries(operation, field, carried, operations, held, spec):
            continue
        found.append(
            f"{where}: {call.get('operation')} does not carry `{field}`: its request carries "
            f"{', '.join(sorted(carried)) or 'nothing'}. Name the object's field that the request carries. "
            f"Where the request takes it under another name and a description says that this request field "
            f"sets `{field}`, record the name under `carried_as`. Otherwise record the call under "
            f"`unmanaged_methods` with the reason: a missing call fails where the operator sees it, and a "
            f"wrong one sends a value the API reads as another field"
        )
    return found


def _carries(
    operation: Any, field: str, carried: set[str], operations: dict[str, Any], held: frozenset[str], spec: Spec
) -> bool:
    """Whether the request carries the object's field: by its name or its last
    name, or as a body of the field's own schema, which holds a field of it
    that is not a field of the object itself."""
    path = field.split(".")
    if path[0] in carried or path[-1] in carried:
        return True
    body = set(operation.request_fields) - held
    if not body:
        return False
    for verb, side in (("create", "request_body"), ("read", "response_body")):
        source = operation_refs.resolve(str(operations.get(verb) or ""), spec)
        if source is None:
            continue
        node = getattr(source, side, None)
        # A read can wrap the object in an envelope such as `data`.
        top = api_spec.nested_fields(source.schemas, node, [])
        prefixes = [[], list(top)] if len(top) == 1 else [[]]
        if any(body & api_spec.nested_fields(source.schemas, node, [*prefix, *path]) for prefix in prefixes):
            return True
    return False


def _shared_field_failures(where: str, entries: list[Any]) -> list[str]:
    """Calls that change the same field, without a different value each. The
    provider sends a change of a field through the call that lists it, and
    two such calls, such as an enable and a disable, differ only in the
    value that they set."""
    sharing: dict[str, list[dict[str, Any]]] = {}
    for call in entries:
        if isinstance(call, dict) and isinstance(call.get("fields"), list):
            for field in dict.fromkeys(str(f) for f in call["fields"]):
                sharing.setdefault(field, []).append(call)
    found: list[str] = []
    for field, shared in sharing.items():
        if len(shared) < 2:
            continue
        names = ", ".join(str(call.get("operation")) for call in shared)
        if not all("value" in call for call in shared):
            found.append(
                f"{where}.update_calls: {names} each change `{field}`. Record under each one "
                f"the `value` of `{field}` that it sets, as its description says, so the "
                f"provider sends the call whose value the configuration asks for"
            )
        elif len({json.dumps(call["value"], sort_keys=True) for call in shared}) < len(shared):
            found.append(
                f"{where}.update_calls: {names} each change `{field}` and state the same value. "
                f"Record the value of `{field}` that each one sets, as its description says"
            )
    return found


def object_fields(operations: dict[str, Any], spec: Spec | None) -> frozenset[str]:
    """The fields the object holds: what its read returns and what its create
    takes. A read can wrap the object in an envelope such as `data`, so the
    read alone does not name them. Empty when neither names a field."""
    if spec is None:
        return frozenset()
    read = operation_refs.resolve(str(operations.get("read") or ""), spec)
    create = operation_refs.resolve(str(operations.get("create") or ""), spec)
    return (read.response_fields if read is not None else frozenset()) | frozenset(
        create.request_fields if create is not None else {}
    )


def _field_failures(where: str, call: Any, held: frozenset[str]) -> list[str]:
    """A field of an update call that is not a field of the object."""
    if not held or not isinstance(call, dict) or not isinstance(call.get("fields"), list):
        return []
    foreign = [str(f) for f in call["fields"] if top_field(f) not in held]
    if not foreign:
        return []
    return [
        f"{where}: {', '.join(foreign)} is not a field of the object, which holds "
        f"{', '.join(sorted(held))}. Name the object's field that the call changes, or a path "
        f"into it such as `metadata.items`. A call that changes no field of the object is not "
        f"an update call: record it under `unmanaged_methods` with the reason"
    ]


def _call_failures(where: str, call: Any, planned: set[Any], seen: set[str], spec: Spec | None) -> list[str]:
    """What breaks one update call."""
    if not isinstance(call, dict):
        return [f"{where}: an update call is a mapping of `operation` and `fields`"]
    found = [f"{where}: `{key}` is not a key of an update call" for key in sorted(set(call) - CALL_KEYS)]
    reference = str(call.get("operation") or "")
    fields = call.get("fields")
    if not isinstance(fields, list) or not fields or not all(str(f).strip() for f in fields):
        found.append(
            f"{where}: `fields` names no field. List each field of the object that "
            f"{reference or 'the call'} changes, so the provider sends a change of "
            f"those fields through it"
        )
    if not reference:
        return [*found, f"{where}: no `operation`"]
    verb = call_verb(reference)
    if verb in VERBS or verb in seen:
        found.append(f"{where}: {reference} gives the verb `{verb}`, which another call of this resource already has")
    seen.add(verb)
    if spec is None:
        return found
    if operation_refs.resolve(reference, spec) is None:
        found.append(f"{where}: {reference} does not resolve against the specifications")
    elif operation_refs.identity(reference, spec) in planned:
        found.append(
            f"{where}: {reference} is already one of the resource's operations, "
            f"so it is not another call that changes the object"
        )
    return found
