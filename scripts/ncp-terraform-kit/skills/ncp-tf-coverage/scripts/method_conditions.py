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

"""`method-conditions`: a plan records what each method's description limits.

A description states what its call cannot do: GCP's `forwardingRules.patch`
"can only patch the network_tier field", `setMachineType` acts on "a stopped
instance", and `disks.resize` can "only increase the size of the disk". A
field's own description can say that it never changes after the create, as
KMS's `purpose` says "Immutable." while the key's patch takes the whole key.
The provider and the live test need each one. Which conditions a description
states is a reading, so the agent records them; that a recorded condition is
in the document is a fact, so its quote is checked against the description of
the method, of a field of its request or of a query parameter.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import api_spec
import destroy_handling
import operation_refs
import resource_shape
from api_spec import Spec

#: Each kind of condition, and what else it must name.
FIELDS = "fields"
MARKER = "deleted_when"
#: A kind that names nothing besides its operation and its quote.
BARE = ""
HANDLING = {
    # Stopping the object costs the operator downtime.
    "needs_stopped": ("refuse", "stop_and_start"),
}
#: The way a `one_way` field can move.
DIRECTIONS = ("increase", "decrease")

KINDS = {
    "changes_only": FIELDS,
    "one_way": FIELDS,
    # A field that no call can change after the create, as its own
    # description says. The quote is the field's, so each listed field holds it.
    "create_only": FIELDS,
    "blocks_delete": FIELDS,
    "needs_stopped": "handling",
    # A delete after which the read still returns the object, with a marker:
    # the provider's wait for a not-found answer never ends, so it reads the
    # marker as gone. A delete whose read answers not found records nothing.
    "marked_deleted": MARKER,
    # An update whose request takes at most one changed field: the provider
    # sends one request for each changed field, so it has no other handling.
    "one_field_per_request": BARE,
    # A field that the update's request carries and that the update's own
    # description says it cannot change: only an update call changes it.
    "cannot_change": FIELDS,
}


def _folded(text: Any) -> str:
    return " ".join(str(text or "").split())


def conditions(resource: dict[str, Any]) -> list[dict[str, Any]]:
    """The resource's conditions, each one a mapping."""
    return [entry for entry in resource.get("conditions") or [] if isinstance(entry, dict)]


def _recorded(resource: dict[str, Any], spec: Spec) -> set[Any]:
    """Each operation the resource sends, by identity."""
    refs = list(resource_shape.verbs(resource).values()) + [str(r) for r in resource.get("entry_methods") or []]
    return {operation_refs.identity(ref, spec) for ref in refs}


@dataclass(frozen=True)
class _Facts:
    """What the documents say about one planned resource, for its conditions."""

    recorded: set[Any]
    held: frozenset[str]
    called: dict[str, str]
    #: Each field that the resource's read returns, as the document writes it.
    read_fields: dict[str, dict[str, Any]]
    #: The identity of the resource's delete, or None.
    delete: Any
    #: The identity of the resource's update, or None.
    update: Any


def _condition_failures(where: str, entry: dict[str, Any], facts: _Facts, spec: Spec) -> list[str]:
    reference = str(entry.get("operation") or "")
    kind = str(entry.get("kind") or "")
    found: list[str] = []
    if kind not in KINDS:
        found.append(f"{where}: kind `{kind}` is not one of {', '.join(KINDS)}")
    elif KINDS[kind] == FIELDS:
        fields = entry.get("fields")
        if not isinstance(fields, list) or not fields or not all(_folded(f) for f in fields):
            found.append(f"{where}: a `{kind}` condition lists the `fields` that it names")
    elif KINDS[kind] == MARKER:
        found += _marker_failures(where, entry, facts, spec)
    elif KINDS[kind] == BARE:
        if entry.get("handling"):
            found.append(
                f"{where}: a `{kind}` condition has no `handling`: the provider sends one request "
                f"for each changed field"
            )
    elif str(entry.get("handling") or "") not in HANDLING[kind]:
        found.append(
            f"{where}: a `{kind}` condition states its `handling`, one of "
            f"{' or '.join(HANDLING[kind])}, for the operator to approve"
        )
    found += _named_failures(where, kind, entry, spec, facts.update)
    if kind == "needs_stopped":
        found += _stopped_failures(where, entry, facts.held)
    operation = operation_refs.resolve(reference, spec)
    if operation is None or operation_refs.identity(reference, spec) not in facts.recorded:
        found.append(
            f"{where}: {reference or 'no operation'} is not an operation, an update call or an "
            f"entry method of this resource"
        )
        return found
    quote = _folded(entry.get("quote"))
    if kind == "create_only":
        return found + _create_only_failures(where, entry, operation, quote, facts.called, spec)
    # A delete that keeps the object is often stated only on what the read
    # returns afterwards: a `deleted` flag, or a state such as DELETE_REQUESTED.
    marked = kind == "marked_deleted"
    texts = described(operation) + (
        [api_spec.value_text(body) for body in facts.read_fields.values()] if marked else []
    )
    if not quote or not any(quote in _folded(text) for text in texts):
        found.append(
            f"{where}: neither the description of {reference} nor a field or a query parameter "
            f"of its request holds the quote {quote!r}"
            + (", nor a field that the read returns" if marked else "")
            + ". Quote the sentence that states the condition, as `methods` prints it"
        )
    return found


def _marker_failures(where: str, entry: dict[str, Any], facts: _Facts, spec: Spec) -> list[str]:
    """A delete after which the read still returns the object: it is the
    resource's delete, it names the field and the value that mark the object
    deleted, and it has no handling, because the delete deletes."""
    found = []
    reference = str(entry.get("operation") or "")
    if facts.delete is None or operation_refs.identity(reference, spec) != facts.delete:
        found.append(f"{where}: a `marked_deleted` condition names the resource's delete, not {reference}")
    if entry.get("handling"):
        found.append(
            f"{where}: a `marked_deleted` delete has no `handling`: the delete deletes, and the "
            f"provider reads the marker as gone"
        )
    marker = entry.get(MARKER)
    if not isinstance(marker, dict) or not _folded(marker.get("field")) or marker.get("value") is None:
        return [
            *found,
            f"{where}: a `marked_deleted` condition names `{MARKER}`, the object's `field` and "
            f"the `value` it holds once the object is deleted, as the documents state it",
        ]
    field = resource_shape.top_field(_folded(marker["field"]))
    if facts.held and field not in facts.held:
        return [
            *found,
            f"{where}: `{MARKER}.field` {field} is not a field of the object, which holds "
            f"{', '.join(sorted(facts.held))}",
        ]
    values = [str(value) for value in (facts.read_fields.get(field) or {}).get("enum") or []]
    if values and str(marker["value"]) not in values:
        found.append(
            f"{where}: `{MARKER}.value` {marker['value']} is not a value of {field}, which holds {', '.join(values)}"
        )
    return found


def described(operation: Any) -> list[str]:
    """Where a condition can be stated: the method's description, the
    description of each field of its request and of the schema that the field
    names, and of each query parameter. Compute's `expandIpCidrRange` says
    only on `ipCidrRange` that the range "can only be larger", Cloud
    Identity's `groups.patch` says only on `updateMask` which fields it may
    name, and GKE's cluster update says only on `ClusterUpdate`, the schema of
    its request's `update`, that a request takes one field."""
    return [
        operation.description,
        *operation.request_fields.values(),
        *field_schemas(operation).values(),
        *operation.query_parameters.values(),
    ]


def field_schemas(operation: Any) -> dict[str, str]:
    """The description of the schema that each field of the request names by
    reference, where the schema has one."""
    found: dict[str, str] = {}
    for name in operation.request_fields:
        field = api_spec.nested_property(operation.schemas, operation.request_body, [name]) or {}
        schema = operation.schemas.get(str(field.get("$ref") or "").rsplit("/", 1)[-1]) if field.get("$ref") else None
        if isinstance(schema, dict) and _folded(schema.get("description")):
            found[name] = str(schema["description"])
    return found


def _create_only_failures(
    where: str, entry: dict[str, Any], operation: Any, quote: str, called: dict[str, str], spec: Spec
) -> list[str]:
    """A field fixed at the create, or a path to a field nested in one, as
    `versionTemplate.protectionLevel`: its own description in the operation's
    request holds the quote, and no update call of the resource changes it or
    the field that holds it."""
    found: list[str] = []
    reference = str(entry.get("operation") or "")
    for field in [str(f) for f in entry.get("fields") or []]:
        head, *rest = field.split(".")
        description = (
            operation.request_fields.get(head)
            if not rest
            else api_spec.nested_description(operation.schemas, operation.request_body, [head, *rest])
        )
        if description is None:
            found.append(
                f"{where}: {reference} does not take `{field}` in its request. Name the operation "
                f"whose request holds the field, and the path to it, and the sentence that fixes it"
            )
        elif not quote or quote not in _folded(description):
            found.append(
                f"{where}: the description of `{field}` in the request of {reference} does not hold "
                f"the quote {quote!r}. A `create_only` field is fixed by its own description: quote "
                f"the sentence that each listed field states, or list the field apart"
            )
        if head in called:
            found.append(
                f"{where}: `{field}` is fixed at the create, and the update call {called[head]} "
                f"changes `{head}`. Read both descriptions again and keep the one the documents state"
            )
    return found


def _named_failures(where: str, kind: str, entry: dict[str, Any], spec: Spec, update: Any) -> list[str]:
    """What a kind names besides its fields and handling: the direction a
    one-way field moves, the calls that stop and start the object, and the
    update whose description excludes a field."""
    reference = str(entry.get("operation") or "")
    if kind == "cannot_change" and (update is None or operation_refs.identity(reference, spec) != update):
        return [f"{where}: a `cannot_change` condition names the resource's update, not {reference}"]
    if kind == "one_way" and entry.get("direction") not in DIRECTIONS:
        return [f"{where}: a `one_way` condition states its `direction`, {' or '.join(DIRECTIONS)}, as the quote says"]
    if kind == "needs_stopped" and entry.get("handling") == "stop_and_start":
        return [
            f"{where}: `stop_and_start` names the `{key}` operation of the object"
            for key in ("stop", "start")
            if operation_refs.resolve(str(entry.get(key) or ""), spec) is None
        ]
    return []


def _stopped_failures(where: str, entry: dict[str, Any], held: frozenset[str]) -> list[str]:
    """The state that means stopped: the provider reads it before the call, so
    a stopped object takes the change and a running one is refused or stopped."""
    state = entry.get("stopped_when")
    if not isinstance(state, dict) or not _folded(state.get("field")) or not _folded(state.get("value")):
        return [
            f"{where}: a `needs_stopped` condition names `stopped_when`, the object's `field` and the "
            f"`value` it holds while the object is stopped, as the documents state it"
        ]
    field = resource_shape.top_field(_folded(state["field"]))
    if held and field not in held:
        return [
            f"{where}: `stopped_when.field` {field} is not a field of the object, which holds {', '.join(sorted(held))}"
        ]
    return []


def stop_and_start(resource: dict[str, Any]) -> list[str]:
    """The stop and start operations that the resource's conditions name."""
    return [
        str(entry.get(key))
        for entry in conditions(resource)
        if entry.get("handling") == "stop_and_start"
        for key in ("stop", "start")
        if entry.get(key)
    ]


def failures(planned: Iterable[tuple[str, dict[str, Any]]], spec: Spec) -> list[str]:
    """One failure for each condition that breaks a rule."""
    found: list[str] = []
    for where, resource in planned:
        operations = resource.get("operations") or {}
        read = operation_refs.resolve(str(operations.get("read") or ""), spec)
        facts = _Facts(
            recorded=_recorded(resource, spec),
            held=resource_shape.object_fields(operations, spec),
            called={
                resource_shape.top_field(str(field)): str(call.get("operation"))
                for call in resource_shape.calls(resource)
                for field in call.get("fields") or []
            },
            read_fields=api_spec.returned_properties(read) if read else {},
            delete=operation_refs.identity(str(operations["delete"]), spec) if operations.get("delete") else None,
            update=operation_refs.identity(str(operations["update"]), spec) if operations.get("update") else None,
        )
        for number, entry in enumerate(conditions(resource)):
            found += _condition_failures(f"{where}.conditions[{number}]", entry, facts, spec)
        found += destroy_handling.failures(where, resource, spec)
    return found


def report_lines(planned: Iterable[dict[str, Any]]) -> list[str]:
    """The report's table of each condition, with its quote and its handling."""
    rows = [
        f"| {resource.get('terraform_type')} | {entry.get('operation')} | {entry.get('kind')} | "
        f"{_cell(_named(entry))} | {_cell(entry.get('handling'))} | {_cell(entry.get('quote'))} |"
        for resource in planned
        for entry in conditions(resource)
    ]
    if not rows:
        return []
    return [
        "",
        "## Conditions and limits in the documents",
        "",
        "What each planned method's description limits, and what the provider does about it. "
        "Each handling is a decision to approve, at the top of this file.",
        "",
        "| type | method | kind | fields | handling | quote |",
        "|---|---|---|---|---|---|",
        *rows,
    ]


def _named(entry: dict[str, Any]) -> str:
    """The fields that a condition names, or the state that means stopped or deleted."""
    for key, words in (("stopped_when", "stopped when"), (MARKER, "deleted when")):
        state = entry.get(key)
        if isinstance(state, dict) and state.get("field"):
            return f"{words} {state.get('field')} = {state.get('value')}"
    return ", ".join(str(f) for f in entry.get("fields") or [])


def _cell(text: Any) -> str:
    return _folded(text).replace("|", "\\|")
