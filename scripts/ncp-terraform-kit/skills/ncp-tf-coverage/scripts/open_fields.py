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

"""`documents_silent`: each place where the plan's facts stop is a decision.

A fact that only a description sentence states is the agent's reading, and a
reading can be skipped: the update's limit, a field fixed at the create, a
call that changes a field. A skipped one leaves the field to the derivation's
default, which is a wrong fact that every other gate passes. So each open
place has a decision. An update states its limit, or that its documents
state none, with the text that it read (see `silent_update`). A field that only the update's body lets change, or that
replaces while a call the plan does not manage could change it, is decided
by a condition, an update call or an override, or is listed as silent. A
sentence that the plan quotes for one field decides each other field that
states it, because the same sentence gives the same fact. A field that only
a request field at another path could change is already left to a reading
that the report shows and the provider step records, so it owes nothing here.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import api_spec
import operation_refs
import replaced_types
import silent_update
from api_spec import Spec

KEY = "documents_silent"
_KEYS = frozenset({"update", "fields"})
#: The kinds whose quote states a fact of a field, not of a call.
FIELD_KINDS = ("create_only", "one_way")
#: The kinds whose quote states a limit of the update.
LIMIT_KINDS = ("changes_only", "cannot_change", "one_field_per_request")
#: How deep the listing shows the fields inside an open field.
NESTED_DEPTH = 3
_SETTABLE = ("required", "optional")
_PARAMETER = "parameter"


def _folded(text: Any) -> str:
    return " ".join(str(text or "").split())


def _settable(attribute: Any) -> bool:
    return bool(set(_SETTABLE) & set(attribute.terraform_flags))


def _walk(attributes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for attribute in attributes:
        path = f"{prefix}{attribute.name}"
        yield path, attribute
        yield from _walk(attribute.attributes, f"{path}.")


def _planned(coverage: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    layers = ((coverage or {}).get("module_plan") or {}).get("layers") or {}
    return [
        (f"module_plan.layers.{layer}.{resource.get('terraform_type', '?')}", resource)
        for layer, body in layers.items()
        if isinstance(body, dict)
        for resource in body.get("resources") or []
        if isinstance(resource, dict)
    ]


def silent(resource: dict[str, Any]) -> dict[str, Any]:
    entry = resource.get(KEY)
    return entry if isinstance(entry, dict) else {}


def _decided(resource: dict[str, Any]) -> set[str]:
    """The fields that a condition, an update call or an override names."""
    named = {
        str(field)
        for condition in resource.get("conditions") or []
        if isinstance(condition, dict)
        for field in condition.get("fields") or []
    }
    named |= {
        str(field)
        for call in resource.get("update_calls") or []
        if isinstance(call, dict)
        for field in call.get("fields") or []
    }
    return named | {
        str(entry.get("field")) for entry in resource.get("field_overrides") or [] if isinstance(entry, dict)
    }


def _unmanaged_calls(resource: dict[str, Any], spec: Spec) -> list[str]:
    """The unmanaged methods that take a body, so they can change a field."""
    found = []
    for entry in resource.get("unmanaged_methods") or []:
        operation = operation_refs.resolve(str((entry or {}).get("operation") or ""), spec)
        if operation is not None and operation.http_method != "GET" and operation.request_schema:
            found.append(str(entry["operation"]))
    return found


def open_fields(resource: dict[str, Any], replaced: Any, schema: Any, spec: Spec) -> dict[str, str]:
    """Each field that the plan leaves open, with why, before any decision."""
    if replaced is None or schema is None:
        return {}
    index = dict(_walk(schema.attributes))
    found = dict.fromkeys(replaced.assumed, "only the update's body lets it change in place")
    calls = _unmanaged_calls(resource, spec)
    if calls:
        for path in replaced.fields:
            attribute = index.get(path)
            if path not in replaced.sources and attribute is not None and attribute.source != _PARAMETER:
                found.setdefault(
                    path, f"it replaces while {', '.join(calls)}, which the plan does not manage, could change it"
                )
    decided = _decided(resource)
    return {path: why for path, why in found.items() if path not in decided}


def _update_failures(where: str, resource: dict[str, Any], spec: Spec) -> list[str]:
    update = str((resource.get("operations") or {}).get("update") or "")
    stated = "update" in silent(resource)
    limits = [
        str(c.get("kind"))
        for c in resource.get("conditions") or []
        if isinstance(c, dict) and c.get("kind") in LIMIT_KINDS and str(c.get("operation")) == update
    ]
    if stated and not update:
        return [f"{where}: `{KEY}.update` says the update states no limit, but the plan has no update"]
    if stated and limits:
        return [f"{where}: a `{limits[0]}` condition limits {update}; take `{KEY}.update` out"]
    if update and not limits and not stated:
        return [
            f"{where}: the update {update} states no limit in the plan. Read its description, its "
            f"request field schemas and its query parameters (`fields` prints them): record "
            f"`changes_only` with the sentence that names the fields it changes, `cannot_change` with the "
            f"sentence that names a field it cannot change, or `one_field_per_request` with the sentence "
            f"that allows one field in each request. When they state none, copy the "
            f"`description` and the `request_field_schemas` that `fields` prints into `{KEY}.update`"
        ]
    operation = operation_refs.resolve(update, spec) if update and stated and not limits else None
    if operation is None:
        return []
    return silent_update.failures(where, update, silent(resource)["update"], operation)


def _shape_failures(where: str, resource: dict[str, Any]) -> list[str]:
    entry = resource.get(KEY)
    if entry is None:
        return []
    if not isinstance(entry, dict) or set(entry) - _KEYS:
        return [
            f"{where}: `{KEY}` holds `update`, the text of an update that states no limit, and a "
            f"`fields` list of field paths"
        ]
    fields = entry.get("fields", [])
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        return [f"{where}: `{KEY}.fields` is a list of field paths"]
    return []


def _field_failures(where: str, resource: dict[str, Any], left_open: dict[str, str]) -> list[str]:
    listed = [str(field) for field in silent(resource).get("fields") or []]
    found = [
        f"{where}: `{path}`: {why}. Read its description (`fields` prints it), and record the condition "
        f"that a sentence states, or list it under `{KEY}.fields` when they state none"
        for path, why in left_open.items()
        if path not in listed
    ]
    found += [
        f"{where}: `{KEY}.fields` lists `{path}`, which the plan does not leave open: a condition, an "
        f"update call or an override decides it, or the update does not carry it"
        for path in listed
        if path not in left_open
    ]
    return found


def _quotes(planned: list[tuple[str, dict[str, Any]]]) -> dict[str, str]:
    """Each sentence that the plan quotes as a fact of a field, with where."""
    found: dict[str, str] = {}
    for _, resource in planned:
        for condition in resource.get("conditions") or []:
            if isinstance(condition, dict) and condition.get("kind") in FIELD_KINDS and condition.get("quote"):
                fields = ", ".join(str(field) for field in condition.get("fields") or [])
                found.setdefault(
                    _folded(condition["quote"]), f"{condition['kind']} of {resource.get('terraform_type')} {fields}"
                )
    return found


def described(resource: dict[str, Any], spec: Spec, path: str, attribute: Any) -> str:
    """The field's own description as the document states it, from the
    request of the create or the update: the provider shortens a long one."""
    for verb in ("create", "update"):
        operation = operation_refs.resolve(str((resource.get("operations") or {}).get(verb) or ""), spec)
        text = (
            api_spec.nested_description(operation.schemas, operation.request_body, path.split("."))
            if operation
            else None
        )
        if text:
            return _folded(text)
    return _folded(getattr(attribute, "description", ""))


def _sentence_failures(
    where: str, resource: dict[str, Any], schema: Any, quotes: dict[str, str], spec: Spec
) -> list[str]:
    if schema is None or not quotes:
        return []
    decided = _decided(resource)
    found: list[str] = []

    def visit(attributes: Iterable[Any], prefix: str) -> None:
        for attribute in attributes:
            path = f"{prefix}{attribute.name}"
            if attribute.forces_replacement or attribute.replaces_when or not _settable(attribute):
                continue
            text = described(resource, spec, path, attribute)
            for quote, origin in quotes.items():
                if path not in decided and quote in text:
                    found.append(
                        f'{where}: `{path}` states "{quote}", which the plan quotes as {origin}. The '
                        f"same sentence gives the same fact: record it for this field too"
                    )
                    break
            visit(attribute.attributes, f"{path}.")

    visit(schema.attributes, "")
    return found


def failures(coverage: dict[str, Any], spec: Spec, spec_files: tuple[Path, ...]) -> list[str]:
    planned = _planned(coverage)
    resources = replaced_types.provider_resources(coverage, spec_files)
    schemas = {resource.terraform_type: resource for resource in resources}
    replaced = {r.terraform_type: r for r in replaced_types.derive(coverage, spec_files, resources)}
    quotes = _quotes(planned)
    found: list[str] = []
    for where, resource in planned:
        shape = _shape_failures(where, resource)
        if shape:
            found += shape
            continue
        kind = str(resource.get("terraform_type"))
        found += _update_failures(where, resource, spec)
        found += _field_failures(where, resource, open_fields(resource, replaced.get(kind), schemas.get(kind), spec))
        found += _sentence_failures(where, resource, schemas.get(kind), quotes, spec)
    return found


def _nested(resource: dict[str, Any], spec: Spec, attribute: Any, path: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for child, nested in _walk(attribute.attributes, f"{path}."):
        if child.count(".") - path.count(".") <= NESTED_DEPTH and _settable(nested):
            text = described(resource, spec, child, nested)
            if text:
                found[child] = text
    return found


def listing(
    coverage: dict[str, Any], spec: Spec, spec_files: tuple[Path, ...], wanted: set[str]
) -> list[dict[str, Any]]:
    """For each planned type: its update with what limits it, and each open
    field with its description and the descriptions of the fields inside it."""
    resources = replaced_types.provider_resources(coverage, spec_files)
    schemas = {resource.terraform_type: resource for resource in resources}
    replaced = {r.terraform_type: r for r in replaced_types.derive(coverage, spec_files, resources)}
    out: list[dict[str, Any]] = []
    for _, resource in _planned(coverage):
        kind = str(resource.get("terraform_type"))
        if wanted and kind not in wanted:
            continue
        entry: dict[str, Any] = {"terraform_type": kind}
        update = operation_refs.resolve(str((resource.get("operations") or {}).get("update") or ""), spec)
        if update is not None:
            entry["update"] = {
                "operation": resource["operations"]["update"],
                **silent_update.read(update),
                "query_parameters": {k: _folded(v) for k, v in update.query_parameters.items()},
            }
        schema = schemas.get(kind)
        index = dict(_walk(schema.attributes)) if schema is not None else {}
        listed = set(silent(resource).get("fields") or [])
        fields = []
        for path, why in open_fields(resource, replaced.get(kind), schema, spec).items():
            attribute = index.get(path)
            item = {
                "field": path,
                "open_because": why,
                "silent": path in listed,
                "description": described(resource, spec, path, attribute),
            }
            nested = _nested(resource, spec, attribute, path) if attribute is not None else {}
            if nested:
                item["nested"] = nested
            fields.append(item)
        entry["fields"] = fields
        out.append(entry)
    return out
