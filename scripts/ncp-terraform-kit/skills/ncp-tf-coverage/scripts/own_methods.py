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

"""`own-methods`: a planned object accounts for every method on its own path.

The methods on an object's item path, and one step below it, as
`<item>/setLabels` or `<item>:addAttestationRule`, are the calls that read or
change that object. Which ones exist is in the paths, so a script lists them;
what each one means is a reading of its description, so the plan records it.
A plan that writes its operations from what the model knows about an API
leaves out the ones the model does not know: GCP's `nodeGroups.patch`, and a
router's route policies, which `updateRoutePolicy` "Updates or creates".

A GET changes nothing, and a creating operation is asked about by the creation
gates, so neither is listed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import api_spec
import method_conditions
import operation_refs
import own_replacements
import resource_shape
from api_spec import Operation, Spec

#: The planned verbs whose path names the object, in the order they are read.
ADDRESSING = ("read", "delete", "update")

#: What `methods` prints for an operation the plan does not record.
NOTHING = "nothing"


def _addressing(operations: dict[str, str], spec: Spec) -> Operation | None:
    """The planned operation whose path names the object, or None."""
    for verb in ADDRESSING:
        operation = operation_refs.resolve(str(operations.get(verb) or ""), spec)
        if operation is not None and operation.normalized_path.endswith(operation_refs.PARAMETER):
            return operation
    return None


def item_path(operations: dict[str, str], spec: Spec) -> str:
    """The normalized path that names the object, or ""."""
    operation = _addressing(operations, spec)
    return operation.normalized_path if operation is not None else ""


def _below(item: str, operation: Operation) -> str | None:
    """The step from the item path to the operation's path: "" on the item
    path, the segment after `/` or `:` one step below, None elsewhere."""
    path = operation.normalized_path
    if path == item:
        return ""
    if len(path) > len(item) + 1 and path.startswith(item) and path[len(item)] in "/:":
        step = path[len(item) + 1 :]
        return None if "/" in step or ":" in step else step
    return None


def name(operation: Operation, item: str) -> str:
    """The method's own name, as a plan-wide reason names it: the last segment
    of a dotted identifier, or else the step below the item path."""
    if "." in operation.operation_id:
        return operation.operation_id.rsplit(".", 1)[-1]
    return _below(item, operation) or operation.operation_id or operation.http_method


def listed(resource: dict[str, Any], spec: Spec) -> tuple[str, list[Operation]]:
    """The object's item path, and each operation on its own path that the
    gate asks about: those of the object's own API, because two APIs can name
    their objects by the same path."""
    operations = {str(v): str(r) for v, r in (resource.get("operations") or {}).items()}
    addressing = _addressing(operations, spec)
    if addressing is None:
        return "", []
    item = addressing.normalized_path
    found = [
        operation
        for operation in spec.every_operation()
        if operation.http_method != "GET"
        and operation.api == addressing.api
        and _below(item, operation) is not None
        and not operation_refs.creates(operation, spec)
    ]
    return item, sorted(found, key=operation_refs.cited)


def _records(resource: dict[str, Any], spec: Spec) -> dict[Any, str]:
    """Each operation the resource records, by identity, with where it is."""
    found: dict[Any, str] = {}
    for verb, reference in (resource.get("operations") or {}).items():
        found.setdefault(operation_refs.identity(str(reference), spec), f"operations.{verb}")
    for call in resource_shape.calls(resource):
        found.setdefault(operation_refs.identity(str(call.get("operation") or ""), spec), "update_calls")
    for reference in resource.get("entry_methods") or []:
        found.setdefault(operation_refs.identity(str(reference), spec), "entry_methods")
    for reference in method_conditions.stop_and_start(resource):
        found.setdefault(operation_refs.identity(reference, spec), "conditions")
    for entry in _unmanaged(resource):
        if str(entry.get("reason") or "").strip():
            found.setdefault(operation_refs.identity(str(entry.get("operation") or ""), spec), "unmanaged_methods")
    return found


def _unmanaged(holder: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in holder.get("unmanaged_methods") or [] if isinstance(entry, dict)]


def _plan_wide(plan: dict[str, Any]) -> dict[str, str]:
    """Each method name a plan-wide reason covers, with the reason."""
    return {
        str(entry.get("method") or ""): str(entry.get("reason") or "").strip()
        for entry in _unmanaged(plan)
        if str(entry.get("reason") or "").strip()
    }


def classify(
    planned: list[tuple[str, dict[str, Any]]], plan: dict[str, Any], spec: Spec
) -> list[tuple[str, dict[str, Any], str, list[tuple[Operation, str]]]]:
    """For each planned resource: its place, the resource, its item path, and
    each listed operation with where the plan records it, or `nothing`."""
    every = [_records(resource, spec) for _, resource in planned]
    wide = _plan_wide(plan)
    out = []
    for index, (where, resource) in enumerate(planned):
        item, operations = listed(resource, spec)
        mine = every[index]
        others = {
            key: value for number, records in enumerate(every) if number != index for key, value in records.items()
        }
        rows = []
        for operation in operations:
            if operation in mine:
                rows.append((operation, mine[operation]))
            elif name(operation, item) in wide:
                rows.append((operation, "module_plan.unmanaged_methods"))
            elif operation in others:
                rows.append((operation, "another planned resource"))
            else:
                rows.append((operation, NOTHING))
        out.append((where, resource, item, rows))
    return out


def failures(planned: list[tuple[str, dict[str, Any]]], plan: dict[str, Any], spec: Spec) -> list[str]:
    """One failure for each planned resource with a method it does not
    record, an unmanaged method with no reason, or entry methods with no note."""
    found = [
        f"module_plan.unmanaged_methods: `{entry.get('method')}` has no reason"
        for entry in _unmanaged(plan)
        if not str(entry.get("reason") or "").strip()
    ]
    for where, resource, _, rows in classify(planned, plan, spec):
        missing = [operation_refs.cited(operation) for operation, place in rows if place == NOTHING]
        if missing:
            found.append(
                f"{where}: the specification has {', '.join(missing)} on this object's own path, and "
                f"the plan does not record it. Read each description with `methods`, then record each "
                f"one as an operation, under `update_calls` with the fields it changes, under "
                f"`entry_methods`, or under `unmanaged_methods` with a reason"
            )
        found += [
            f"{where}: `unmanaged_methods` names {entry.get('operation')} with no reason"
            for entry in _unmanaged(resource)
            if not str(entry.get("reason") or "").strip()
        ]
        if resource.get("entry_methods") and not str(resource.get("entries_note") or "").strip():
            found.append(
                f"{where}: `entry_methods` is recorded and `entries_note` is empty. Say how "
                f"the resource manages those entries"
            )
    return found


def replacements(planned: list[dict[str, Any]], plan: dict[str, Any], replaced: list[Any], spec: Spec) -> list[str]:
    """The decisions to approve: each replacement that an unmanaged method of
    the object could avoid."""
    classified = classify([("", resource) for resource in planned], plan, spec)
    return own_replacements.lines(classified, replaced, spec, name)


def methods(planned: list[tuple[str, dict[str, Any]]], plan: dict[str, Any], spec: Spec) -> list[dict[str, Any]]:
    """What the `methods` command prints: each resource's listed operations."""
    return [
        {
            "terraform_type": resource.get("terraform_type"),
            "item_path": item,
            "methods": [
                {
                    "operation": operation_refs.cited(operation),
                    "http_method": operation.http_method,
                    "path": operation.path,
                    "recorded": place,
                    "description": operation.description,
                    "request_fields": {name: text for name, text in operation.request_fields.items() if text},
                    **_field_schemas(operation),
                    "query_parameters": {name: text for name, text in operation.query_parameters.items() if text},
                }
                for operation, place in rows
            ],
            **_read_returns(resource, spec),
        }
        for _, resource, item, rows in classify(planned, plan, spec)
    ]


def _field_schemas(operation: Operation) -> dict[str, Any]:
    """The description of each schema that a request field names, where one
    states something: a limit can be stated only there."""
    found = method_conditions.field_schemas(operation)
    return {"request_field_schemas": found} if found else {}


def _read_returns(resource: dict[str, Any], spec: Spec) -> dict[str, Any]:
    """What the resource's read returns, field by field: whether a delete keeps
    the object is often stated only there, as a `deleted` flag or a state."""
    read = operation_refs.resolve(str((resource.get("operations") or {}).get("read") or ""), spec)
    returned = {name: text for name, text in api_spec.returned_descriptions(read).items() if text} if read else {}
    return {"read_returns": returned} if returned else {}


def report_lines(planned: Iterable[dict[str, Any]], plan: dict[str, Any]) -> list[str]:
    """The report's table of each method the plan does not manage, with its reason."""
    rows = [f"| every type | {entry.get('method')} | {_cell(entry.get('reason'))} |" for entry in _unmanaged(plan)]
    rows += [
        f"| {resource.get('terraform_type')} | {entry.get('operation')} | {_cell(entry.get('reason'))} |"
        for resource in planned
        for entry in _unmanaged(resource)
    ]
    if not rows:
        return []
    return [
        "",
        "## Methods the plan does not manage",
        "",
        "Each method on a planned object's own path that no resource sends, and why. "
        "A method that is left out is a capability that the provider does not have.",
        "",
        "| type | method | reason |",
        "|---|---|---|",
        *rows,
    ]


def _cell(text: Any) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")
