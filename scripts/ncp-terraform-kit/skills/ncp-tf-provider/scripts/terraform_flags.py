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
"""The Terraform flags of each attribute, derived from its behaviour and the read.

`behaviour` is what the document says about a field. The flags are what the
runtime sets, and two Terraform facts make them differ from it:

- Core keeps the prior value of an optional and computed nested attribute that
  the configuration leaves unset only when every attribute inside it is
  computed. One non-computed child makes core propose null, and the provider
  then plans to remove what the read returned.
- The framework refuses a non-computed attribute inside a computed-only one.

Whether the API fills a field that the configuration leaves unset is read from
the read operation's response: the field is stated there, at the same path.
The rule reads only structure that both specification languages state.

Pure: plain data in, plain data out. The caller resolves the read operation and
passes its document's schema table.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

REQUIRED: tuple[str, ...] = ("required",)
OPTIONAL: tuple[str, ...] = ("optional",)
COMPUTED: tuple[str, ...] = ("computed",)
OPTIONAL_COMPUTED: tuple[str, ...] = ("optional", "computed")

#: A document without a write-only flag says it in prose, in the forms it uses
#: for the output-only marker. A clause of its own, as that marker is:
#: `[Output only for type PARTNER. Input only for PARTNER_PROVIDER]` qualifies a
#: case, not the field.
INPUT_ONLY = re.compile(r"(?:^|[.;:(]\s*)\[?input[ _-]?only\s*(?:[.)\]]|$)", re.IGNORECASE)

Path = tuple[str, ...]


def is_input_only(prop: dict[str, Any]) -> bool:
    """Whether the document says that the API never returns this field."""
    if prop.get("writeOnly") is True:
        return True
    return bool(INPUT_ONLY.search(str(prop.get("description") or "").strip()))


def _object(prop: Any, table: dict[str, Any]) -> dict[str, Any] | None:
    """The object schema a property or its items resolve to, when it states fields."""
    if not isinstance(prop, dict):
        return None
    for node in (prop, prop.get("items")):
        if not isinstance(node, dict):
            continue
        reference = node.get("$ref")
        target = table.get(str(reference).rsplit("/", 1)[-1]) if reference else node
        if isinstance(target, dict) and isinstance(target.get("properties"), dict):
            return target
    return None


def _named(prop: dict[str, Any]) -> str:
    reference = prop.get("$ref") or (prop.get("items") or {}).get("$ref") or ""
    return str(reference).rsplit("/", 1)[-1]


def _is_object(prop: Any) -> bool:
    return isinstance(prop, dict) and (prop.get("type") == "object" or bool(prop.get("$ref")))


def stated_paths(response_ref: str, table: dict[str, Any], top_names: set[str], max_depth: int) -> set[Path]:
    """Every field path the read's response states, from the resource's top.

    A response that holds exactly one field, an object that is not a field the
    request sends, is an envelope: the paths start inside it, and an envelope
    that states no fields states nothing. An API that wraps every answer would
    otherwise state one field that no attribute has. An input-only field is not
    stated, nor anything under it.
    """
    root = table.get(response_ref) if response_ref else None
    if not isinstance(root, dict):
        return set()
    properties = root.get("properties") or {}
    if len(properties) == 1:
        ((name, only),) = properties.items()
        if _is_object(only) and name not in top_names:
            inner = _object(only, table)
            if inner is None:
                return set()
            root = inner
    found: set[Path] = set()
    _walk(root, table, (), frozenset({response_ref}), max_depth, found)
    return found


def inside_envelope(returned: list[Any], settable: set[str], operation: str) -> tuple[list[Any], list[str]]:
    """The fields a create's response states, from inside its envelope.

    The same rule as `stated_paths`: a response of exactly one object field
    that the request does not send is an envelope, not a field of the resource.
    """
    if len(returned) != 1 or returned[0].type != "object" or returned[0].name in settable:
        return returned, []
    (envelope,) = returned
    if envelope.attributes:
        return list(envelope.attributes), []
    return [], [
        f"{operation} answers with one field, {envelope.name!r}, that the request does not "
        f"send: an envelope that states no field of the resource, so the response adds no "
        f"attribute"
    ]


def _walk(
    schema: dict[str, Any],
    table: dict[str, Any],
    prefix: Path,
    open_names: frozenset[str],
    max_depth: int,
    found: set[Path],
) -> None:
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict) or is_input_only(prop):
            continue
        path = (*prefix, name)
        found.add(path)
        named = _named(prop)
        child = _object(prop, table)
        # The same bounds as the expansion of the create body: a schema already
        # open on this path is a cycle, and the depth stops a deep document.
        if child is None or named in open_names or len(path) >= max_depth:
            continue
        _walk(child, table, path, open_names | {named} if named else open_names, max_depth, found)


def _flags(behaviour: str, enclosing: tuple[str, ...], stated: bool) -> tuple[str, ...]:
    """The first rule that matches, from the top down."""
    if enclosing == COMPUTED or behaviour == "computed":
        # The framework fact.
        return COMPUTED
    if enclosing == OPTIONAL_COMPUTED:
        # The core fact: one non-computed child, and core proposes null for a
        # value the configuration left unset, which removes what the read filled.
        return OPTIONAL_COMPUTED
    if behaviour == "required":
        return REQUIRED
    return OPTIONAL_COMPUTED if stated else OPTIONAL


def for_resource(
    attributes: list[Any],
    read: Any,
    table: dict[str, Any],
    wrapper: dict[str, str],
    settable: set[str],
    max_depth: int,
) -> tuple[list[Any], list[str]]:
    """The attributes with their flags, and a note when no read says which
    fields the API fills.

    `read` is the resolved read operation, or None. `settable` names the
    top-level fields that a request sends, which an envelope cannot be.
    """
    if read is None:
        stated: set[Path] = set()
        note = "no read operation is planned or found, so no attribute is computed because of a read"
    else:
        stated = stated_paths(read.response_ref, table, settable, max_depth)
        note = (
            ""
            if stated
            else (
                f"{read.operation_id} states no field of the resource in its response, so no "
                f"attribute is computed because of the read"
            )
        )
    return assign(attributes, stated, wrapper.get("field", "")), [note] if note else []


def assign(attributes: list[Any], stated: set[Path], wrapper: str = "") -> list[Any]:
    """Each attribute with its `terraform_flags`, at every depth.

    `wrapper` is the field a create body nests its payload under. The read
    answers with the payload, so a path inside the wrapper is compared from the
    read's top, and the wrapper is stated when the read states any field.
    """

    def is_stated(path: Path) -> bool:
        if wrapper and path[0] == wrapper:
            return path[1:] in stated if len(path) > 1 else bool(stated)
        return path in stated

    return [_assigned(a, (a.name,), (), is_stated) for a in attributes]


def _assigned(attribute: Any, path: Path, enclosing: tuple[str, ...], is_stated: Callable[[Path], bool]) -> Any:
    flags = _flags(attribute.behaviour, enclosing, is_stated(path))
    return replace(
        attribute,
        terraform_flags=flags,
        attributes=tuple(_assigned(child, (*path, child.name), flags, is_stated) for child in attribute.attributes),
    )


def settable_through_calls(attributes: list[Any], fields: set[str], wrapper: str = "") -> list[Any]:
    """Each attribute that the document marks read-only and an update call
    changes, made settable and marked `set_by_calls`.

    `fields` are the object's fields that the update calls name, or paths into
    them. The API sets such a field only through its call, never in a create or
    update body, so the configuration must be able to ask for a value, and the
    runtime sends the call that sets it. A path inside the create's `wrapper`
    names the payload's field, as the read answers with the payload.
    """
    paths = set()
    tops = {a.name for a in attributes}
    for field in fields:
        path = tuple(str(field).split("."))
        if path[0] not in tops and wrapper:
            path = (wrapper, *path)
        paths.add(path)
    return [_settable(a, (a.name,), (), paths) for a in attributes]


def _settable(attribute: Any, path: Path, enclosing: tuple[str, ...], paths: set[Path]) -> Any:
    flags = attribute.terraform_flags
    called = path in paths and flags == COMPUTED and enclosing != COMPUTED
    if called:
        flags = OPTIONAL_COMPUTED
    return replace(
        attribute,
        terraform_flags=flags,
        set_by_calls=called or getattr(attribute, "set_by_calls", False),
        attributes=tuple(_settable(child, (*path, child.name), flags, paths) for child in attribute.attributes),
    )
