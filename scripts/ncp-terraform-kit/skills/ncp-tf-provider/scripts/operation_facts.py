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
"""What each planned operation tells the provider, read from the documents.

The provider's runtime is written once and serves every resource. What differs
between resources is here, as data: the path template an identifier is matched
against, whether an update replaces the whole object or patches part of it,
which field mask it declares, which fingerprint it must echo, and whether the
body wraps the resource under a key. A runtime that reads these facts needs no
branch on an operation's name, and a runtime that branches on names is correct
for the resources somebody remembered and wrong for the next one.

Pure: plain data in, plain data out. The caller resolves references and passes
each method's own schema table.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

#: Names a field mask goes by. A document states the mask as a parameter or a
#: body field and gives it no type of its own, so the name is the evidence.
MASK_NAMES = frozenset({"updateMask", "update_mask", "fieldMask", "field_mask", "mask", "paths"})

#: The fingerprint a changing call must echo, and the one a label change must.
FINGERPRINT = "fingerprint"
LABEL_FINGERPRINT = "labelFingerprint"

#: What makes a fingerprint a concurrency token rather than a value. The name is
#: not enough: a key's `fingerprint` is the key's own hash, and echoing it on an
#: update means nothing. The document says which kind it is, in the description:
#: a token is "used for optimistic locking" or "used to detect conflicts".
LOCKING = ("lock", "conflict")

#: The other way a document says the API checks a token: a value "must be
#: provided", and a request whose value is "out of date" fails. Each phrase
#: alone fits a plain required field, so a token needs both.
MUST_BE_CURRENT = ("must be provided", "out of date")

#: Where a parameter that names a list item can stand: the call's own address.
ITEM_PARAMETER_LOCATIONS = frozenset({"path", "query"})


class Parameter(Protocol):
    name: str
    location: str
    required: bool
    repeated: bool
    pattern: str


class Method(Protocol):
    operation_id: str
    http_method: str
    path: str
    template: str
    service_path: str
    parameters: tuple[Parameter, ...]
    request_ref: str
    response_ref: str
    document: str
    async_reason: str
    description: str


def for_resource(
    operations: dict[str, str],
    resolve: Callable[[str], Method | None],
    schemas: dict[str, dict[str, Any]],
    *,
    reported: frozenset[str] = frozenset(),
    provider_value: Callable[[Parameter], str] = lambda parameter: "",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Every planned operation of one resource, described, and what could not be.

    `schemas` is the per-document table: a reference means something only in
    the document that wrote it. `reported` names verbs whose absence the caller
    already reports, so it is not said twice. `provider_value` gives what the
    provider configuration sends for a parameter, or "".
    """
    read = resolve(str(operations.get("read") or "")) if operations.get("read") else None
    resource_schema = read.response_ref if read else ""
    facts: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for verb, reference in sorted(operations.items()):
        method = resolve(str(reference))
        if method is None:
            if verb not in reported:
                notes.append(f"{verb} operation {reference!r} is not in the supplied documents")
            continue
        entry, found = describe(method, schemas.get(method.document, {}), resource_schema, provider_value)
        facts[verb] = entry
        notes.extend(f"{verb}: {note}" for note in found)
    return facts, notes


def describe(
    method: Method,
    table: dict[str, Any],
    resource_schema: str,
    provider_value: Callable[[Parameter], str] = lambda parameter: "",
) -> tuple[dict[str, Any], list[str]]:
    """One operation as the runtime needs it, and anything a person must decide."""
    body = table.get(method.request_ref) if method.request_ref else None
    fields = (body or {}).get("properties") or {}
    entry: dict[str, Any] = {
        "operation": method.operation_id,
        "http_method": method.http_method,
        # The template, not a flattened spelling: `{+name}` says the value spans
        # several segments, and matching a stored id needs to know it.
        "path": method.template or method.path,
        "service_path": method.service_path,
        "parameters": [_parameter(p, provider_value(p)) for p in method.parameters],
        "request_schema": method.request_ref,
        "response_schema": method.response_ref,
        "long_running": bool(method.async_reason),
    }
    if method.description:
        entry["description"] = method.description
    notes: list[str] = []

    masks = field_masks(method, fields)
    if masks:
        entry["field_masks"] = masks
    if len(masks) > 1:
        notes.append(
            f"{method.operation_id} declares {len(masks)} field masks "
            f"({', '.join(m['name'] for m in masks)}). Which one the API honours is "
            f"not in the document; decide it, and record the decision for review"
        )
    notes.extend(fingerprints(entry, method, fields, resource_schema, table))
    if method.http_method == "PUT" and method.request_ref and method.request_ref == resource_schema:
        entry["full_replacement"] = True
    wrapper = wrapped_under(fields, method.request_ref, resource_schema)
    lone = lone_reference(fields)
    if wrapper:
        entry["wraps_resource_under"] = wrapper
    elif lone and method.request_ref and method.request_ref != resource_schema:
        notes.append(
            f"{method.operation_id} sends {method.request_ref}, which holds one schema field, "
            f"`{lone[0]}`, beside fields that are not identifying strings, so it is not marked "
            f"wraps_resource_under. Whether the body carries its payload under `{lone[0]}` or is "
            f"the payload itself is your reading of the field descriptions; record the decision "
            f"for review"
        )
    return entry, notes


def _parameter(parameter: Parameter, provider_value: str) -> dict[str, Any]:
    """One parameter, with the value the provider sends when the caller cannot."""
    out: dict[str, Any] = {
        "name": parameter.name,
        "location": parameter.location,
        "required": parameter.required,
        "repeated": parameter.repeated,
    }
    if provider_value:
        # A template over the provider settings, as `projects/{project}`. The
        # runtime reads it here, so no runtime keeps a list of parameter names.
        out["provider_value"] = provider_value
    return out


def is_locking_token(spec: Any) -> bool:
    """Whether a field is one the API checks to refuse a stale write."""
    if not isinstance(spec, dict):
        return False
    description = " ".join(str(spec.get("description") or "").lower().split())
    return any(word in description for word in LOCKING) or all(phrase in description for phrase in MUST_BE_CURRENT)


def fingerprints(
    entry: dict[str, Any],
    method: Method,
    fields: dict[str, Any],
    resource_schema: str,
    table: dict[str, Any],
) -> list[str]:
    """Record each locking token the request echoes, with where the read holds
    it, and list each token whose place the schemas leave open."""
    notes: list[str] = []
    for token, key in ((FINGERPRINT, "fingerprint"), (LABEL_FINGERPRINT, "label_fingerprint")):
        if not is_locking_token(fields.get(token)):
            continue
        entry[f"{key}_field"] = token
        path, places = read_path(token, method.request_ref, resource_schema, table, method.parameters)
        if path:
            entry[f"{key}_read_path"] = path
            continue
        notes.append(
            f"{method.operation_id} sends `{token}` in {method.request_ref}, and the read of "
            f"{resource_schema or 'the resource'} holds it at no single place "
            f"({', '.join(places) or 'none found'}). Where the runtime reads its current "
            f"value is your reading of the document; record the decision for review"
        )
    return notes


def read_path(
    token: str,
    request_ref: str,
    resource_schema: str,
    table: dict[str, Any],
    parameters: tuple[Parameter, ...] = (),
) -> tuple[str, list[str]]:
    """Where the read holds the current value of a token the request echoes,
    or "" with the places that leave it open.

    A body that is the resource holds the token at the top level. A body that
    is the schema of one resource field holds the token of that field: Compute's
    `setMetadata` sends `Metadata`, so the instance's `metadata.fingerprint`,
    not its own `fingerprint`. Another body, as a `SetLabelsRequest`, carries
    the token that the read holds at one place. A place inside a list item is
    the answer only when the call names the item: see `item_selected`.
    """
    if not resource_schema:
        return "", []
    if request_ref == resource_schema:
        return token, [token]
    holders = _places(resource_schema, table, lambda name, ref: ref == request_ref)
    if holders:
        places = [f"{holder}.{token}" for holder in holders]
    else:
        places = _places(resource_schema, table, lambda name, ref: name == token)
    if len(places) == 1 and "[]" not in places[0]:
        return places[0], places
    if len(places) == 1:
        return item_selected(places[0], resource_schema, table, parameters), places
    return "", places


def item_selected(place: str, resource_schema: str, table: dict[str, Any], parameters: tuple[Parameter, ...]) -> str:
    """The place with its one list item chosen by the call's parameter, or "".

    The schemas alone do not say which item a call changes. A call says it
    when the item has a `name` and a required parameter of the call bears the
    item schema's own name, as Compute's `updateNetworkInterface` takes
    `networkInterface` for an item of `networkInterfaces`. The place becomes
    `networkInterfaces[name == {networkInterface}].fingerprint`: the item whose
    `name` equals the value the call sends in that parameter.
    """
    segments = place.split(".")
    listed = [i for i, segment in enumerate(segments) if segment.endswith("[]")]
    if len(listed) != 1:
        return ""
    schema = resource_schema
    for segment in segments[: listed[0] + 1]:
        spec = ((table.get(schema) or {}).get("properties") or {}).get(segment.removesuffix("[]"))
        node = spec.get("items") if isinstance(spec, dict) and segment.endswith("[]") else spec
        schema = str((node or {}).get("$ref") or "").rsplit("/", 1)[-1] if isinstance(node, dict) else ""
    if not schema or "name" not in ((table.get(schema) or {}).get("properties") or {}):
        return ""
    bearer = schema[0].lower() + schema[1:]
    if not any(p.name == bearer and p.required and p.location in ITEM_PARAMETER_LOCATIONS for p in parameters):
        return ""
    segments[listed[0]] = f"{segments[listed[0]][:-2]}[name == {{{bearer}}}]"
    return ".".join(segments)


def _places(
    schema: str,
    table: dict[str, Any],
    match: Callable[[str, str], bool],
    prefix: str = "",
    seen: frozenset[str] = frozenset(),
) -> list[str]:
    """Every path in a schema whose field matches, by name or by referenced schema.

    A path through a list item is written `name[]`. A schema already on the
    path is not entered again, so a recursive schema ends.
    """
    found: list[str] = []
    for name, spec in sorted(((table.get(schema) or {}).get("properties") or {}).items()):
        if not isinstance(spec, dict):
            continue
        listed = spec.get("type") == "array" and isinstance(spec.get("items"), dict)
        node = spec["items"] if listed else spec
        ref = str(node.get("$ref") or "").rsplit("/", 1)[-1]
        path = f"{prefix}{name}{'[]' if listed else ''}"
        if match(name, ref):
            found.append(path)
        if ref and ref not in seen | {schema}:
            found += _places(ref, table, match, f"{path}.", seen | {schema})
    return found


def field_masks(method: Method, fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field mask the operation declares, as a parameter or a body field."""
    found = [
        {"name": p.name, "location": p.location, "repeated": p.repeated}
        for p in method.parameters
        if p.name in MASK_NAMES
    ]
    found += [
        {"name": name, "location": "body", "repeated": (spec or {}).get("type") == "array"}
        for name, spec in sorted(fields.items())
        if name in MASK_NAMES
    ]
    return found


def wrapped_under(fields: dict[str, Any], request_ref: str, resource_schema: str) -> dict[str, str] | None:
    """The key a body nests its payload under, when it does.

    A body that is not the resource and holds exactly one field naming another
    schema carries its payload there: `{"update": ClusterUpdate}` beside a few
    identifying strings. Sent flat, every field of the payload lands at the top
    level, where the API ignores it or refuses it. A body with any other kind
    of field beside it, such as a rule's priority and logging flag beside its
    `match`, is the payload itself.
    """
    if not request_ref or request_ref == resource_schema:
        return None
    lone = lone_reference(fields)
    if lone is None:
        return None
    name, schema = lone
    if any(
        not isinstance(spec, dict) or spec.get("type") != "string" for other, spec in fields.items() if other != name
    ):
        return None
    return {"field": name, "schema": schema}


def lone_reference(fields: dict[str, Any]) -> tuple[str, str] | None:
    """The body's one field that names a schema, with the schema, or None."""
    referenced = [
        (name, str(spec["$ref"]).rsplit("/", 1)[-1])
        for name, spec in sorted(fields.items())
        if isinstance(spec, dict) and spec.get("$ref")
    ]
    return referenced[0] if len(referenced) == 1 else None


#: Body fields that steer a request rather than carry the resource: masks and
#: concurrency tokens.
STEERING = MASK_NAMES | {FINGERPRINT, LABEL_FINGERPRINT, "etag"}


def steers(name: str, description: str) -> bool:
    """Whether a resource field guards or shapes a write rather than describes
    the object. A fingerprint steers only when its description says it guards:
    a key's `fingerprint` is the key's own hash."""
    if name in (FINGERPRINT, LABEL_FINGERPRINT):
        return is_locking_token({"description": description})
    return name in STEERING


def _payload_level(attributes: Any, create: dict[str, Any] | None) -> set[str]:
    """The resource's fields at the paths an update payload holds them.

    That is the resource's top-level attributes, and the fields of the payload
    its create wraps. A name found deeper is a field at another path: the
    payload does not hold it there, however the name matches.
    """
    wrapped = ((create or {}).get("wraps_resource_under") or {}).get("field")
    found: set[str] = set()
    for attribute in attributes or ():
        found.add(str(attribute.name))
        if attribute.name == wrapped:
            found |= {str(child.name) for child in getattr(attribute, "attributes", ())}
    return found


def payload_fields(
    entry: dict[str, Any],
    table: dict[str, Any],
    output_only: Callable[[dict[str, Any]], bool] = lambda spec: spec.get("readOnly") is True,
) -> set[str]:
    """The fields an update's payload changes: the wrapped schema's when the
    body wraps one, none that steer the request, and none that the document
    marks output-only. A body that echoes an output-only field, as Compute's
    instance update echoes its zone, does not change it."""
    wrapper = entry.get("wraps_resource_under") or {}
    payload = wrapper.get("schema") or entry.get("request_schema") or ""
    properties = (table.get(payload) or {}).get("properties") or {}
    inline = [
        spec for spec in properties.values() if isinstance(spec, dict) and isinstance(spec.get("properties"), dict)
    ]
    if not wrapper and len(inline) == 1 and len(properties) - len(set(properties) & STEERING) == 1:
        # The same wrapping as `wraps_resource_under`, written inline rather
        # than by reference: the payload is the one object field.
        properties = inline[0]["properties"]
    return {
        name
        for name, spec in properties.items()
        if not (isinstance(spec, dict) and output_only(spec))
        and not steers(name, str((spec or {}).get("description") or "") if isinstance(spec, dict) else "")
    }


def mark_call(entry: dict[str, Any], table: dict[str, Any]) -> list[str]:
    """List each field of an update call's request that is not a field it changes.

    The plan names the object's fields that the call changes, and the request
    can take the value under another name or in the query: a resize takes
    `size_gb` for `disk_size_gb`, and Compute's `setDeletionProtection` takes
    its value as a query parameter. Which attribute fills such a field is a
    reading of the document, so it is listed for the agent and not mapped here.
    """
    changed = {str(field).split(".", 1)[0] for field in entry.get("fields") or []}
    query = {
        str(p.get("name"))
        for p in entry.get("parameters") or []
        if isinstance(p, dict) and p.get("location") == "query"
    } - STEERING
    off_path = sorted((payload_fields(entry, table) | query) - changed)
    if not off_path:
        return []
    entry["request_fields_off_path"] = off_path
    return [
        f"{entry.get('operation')} changes {', '.join(sorted(changed))} and its request carries "
        f"{', '.join(off_path)} at another path (request_fields_off_path). For each, record the "
        f"attribute that fills it, with its source, or that none does"
    ]


def mark_update(
    entry: dict[str, Any],
    table: dict[str, Any],
    attributes: Any,
    *,
    create: dict[str, Any] | None = None,
) -> list[str]:
    """List each update request field that is not a resource field at its path.

    The runtime cannot fill such a field from the attribute of the same path,
    and an attribute sent at its own path lands where the request ignores or
    refuses it. Which attribute a listed field changes is a reading of the
    document, so it is listed for the agent and not mapped here. The payload is
    the wrapped schema when the body wraps one.
    """
    payload = (entry.get("wraps_resource_under") or {}).get("schema") or entry.get("request_schema") or ""
    fields = payload_fields(entry, table)
    off_path = sorted(fields - _payload_level(attributes, create))
    if not off_path:
        return []
    entry["request_fields_off_path"] = off_path
    return [
        f"update: {entry.get('operation')} sends {payload}, and {len(off_path)} of its "
        f"{len(fields)} fields are not resource fields at the same path "
        f"(request_fields_off_path). For each attribute the update changes and the "
        f"payload does not hold at the attribute's path, record the request path that "
        f"changes it, with its source, and refuse an attribute the record does not hold"
    ]
