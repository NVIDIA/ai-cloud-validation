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
"""The interface a reviewer chose for a layer module, beyond its references.

The sample modules name what they create `<prefix>-<key>` unless an entry names
itself, and take a scope -- a project, a region -- once for the module, as a
default each entry may override. Which attribute is a resource's name, and
which ones carry the same scope, is a reading of the NCP: one calls the name
`label`, another nests it in an object, another calls the region
`region_name`. So a person states it in `references.yaml`, and this applies it:

    variables:
      <terraform_type>: <the cross-cloud name of its variable and output>
    name_attribute:
      <terraform_type>: <attribute>, or <object>.<attribute> in a nested object
    shared:
      <module input>:
        <terraform_type>: <attribute>, or <object>.<attribute>

A resource `variables` does not list takes the plural of its logical kind. A
resource neither of the other two sections lists gets no default. A shared
input that fills a reference to another layer is that layer's id input.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PREFIX = "prefix"
#: Terraform's own rule for a variable name, narrowed to the lower case every
#: module variable here uses.
INPUT_NAME = re.compile(r"[a-z][a-z0-9_]*")
#: Names Terraform reserves for a module call's own arguments, so no variable
#: can take them.
RESERVED = frozenset({"count", "depends_on", "for_each", "lifecycle", "locals", "providers", "source", "version"})
#: The name default; a conditional, not coalesce(), which also skips "".
NAME_DEFAULT = f'var.{PREFIX} != null ? "${{var.{PREFIX}}}-${{each.key}}" : each.key'


@dataclass
class Shape:
    #: terraform_type -> {path of an attribute: the expression it defaults to}.
    defaults: dict[str, dict[tuple[str, ...], str]] = field(default_factory=dict)
    named: bool = False
    #: module input -> (its Terraform JSON type, the resource types it fills).
    shared: dict[str, tuple[Any, list[str]]] = field(default_factory=dict)
    #: terraform_type -> its variable and output, where a reviewer renamed it.
    variables: dict[str, str] = field(default_factory=dict)
    #: (terraform_type, path of a nested object) for each object that is a list
    #: or a set, so a default is written into every element.
    lists: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)


def variable_of(shape: Shape, resource: dict[str, Any], pluralize: Callable[[str], str]) -> str:
    """The variable, and the output, that holds one resource type's entries."""
    return shape.variables.get(resource["terraform_type"]) or pluralize(resource["logical_kind"])


def _input_name(name: Any) -> bool:
    return bool(INPUT_NAME.fullmatch(str(name))) and name not in RESERVED and name != PREFIX


def _attributes(schemas: dict[str, Any], terraform_type: str) -> dict[str, Any]:
    return (schemas.get(terraform_type) or {}).get("block", {}).get("attributes", {})


def _settable(defn: dict[str, Any] | None) -> bool:
    return bool(defn) and bool(defn.get("required") or defn.get("optional"))


def _leaf(
    attributes: dict[str, Any],
    path: list[str],
    lists: set[tuple[str, ...]] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The attribute at `path`, or why a default cannot be written there.

    Each nested object on the way that is a list or a set is added to `lists`.
    """
    *objects, last = path
    for depth, part in enumerate(objects):
        defn = attributes.get(part)
        nested = (defn or {}).get("nested_type") or {}
        mode = nested.get("nesting_mode", "single")
        if _settable(defn) and mode == "map":
            return None, f"{part} is a map of objects; a default cannot be written into it"
        if not _settable(defn) or not nested or mode not in ("single", "list", "set"):
            return None, f"{part} is not a nested object a caller sets"
        if mode != "single" and lists is not None:
            lists.add(tuple(path[: depth + 1]))
        attributes = nested.get("attributes", {})
    defn = attributes.get(last)
    if not _settable(defn):
        return None, f"{last} is not an attribute a caller sets"
    if "type" not in defn:
        return None, f"{last} is an object; name an attribute inside it"
    return defn, None


def read_shape(
    review: dict[str, Any],
    resources: list[dict[str, Any]],
    schemas: dict[str, Any],
    pluralize: Callable[[str], str],
    fail: Callable[[str], None],
) -> Shape:
    """The reviewed interface, refused where the schema cannot carry it."""
    here = {r["terraform_type"] for r in resources}
    shape = Shape()

    for terraform_type, name in (review.get("variables") or {}).items():
        where = f"references.yaml: variables: {terraform_type}"
        if terraform_type not in here:
            fail(f"{where}: this layer does not generate {terraform_type}")
        if not _input_name(name):
            fail(f"{where}: {name!r} is not a name a module variable can take")
        shape.variables[str(terraform_type)] = str(name)
    variables = [variable_of(shape, r, pluralize) for r in resources]
    if len(set(variables)) != len(variables):
        fail(f"references.yaml: variables: two resource types share one name in {sorted(variables)}")

    def place(where: str, terraform_type: str, dotted: str, default: str) -> Any:
        if terraform_type not in here:
            fail(f"{where}: this layer does not generate {terraform_type}")
        path = str(dotted).split(".")
        found: set[tuple[str, ...]] = set()
        defn, problem = _leaf(_attributes(schemas, terraform_type), path, found)
        shape.lists.update((terraform_type, prefix) for prefix in found)
        if problem:
            fail(f"{where}: {problem}")
        slots = shape.defaults.setdefault(terraform_type, {})
        if tuple(path) in slots:
            fail(f"{where}: {terraform_type}.{dotted} already has a default")
        slots[tuple(path)] = default
        return (defn or {}).get("type")

    for terraform_type, dotted in (review.get("name_attribute") or {}).items():
        where = f"references.yaml: name_attribute: {terraform_type}"
        if place(where, str(terraform_type), dotted, NAME_DEFAULT) != "string":
            fail(f"{where}: {dotted} is not a string, and the name default is one")
        shape.named = True

    for name, targets in (review.get("shared") or {}).items():
        where = f"references.yaml: shared: {name}"
        if not _input_name(name) or name in variables:
            fail(f"{where}: not a name a module input can take here")
        types = {
            json.dumps(place(where, str(tt), dotted, f"var.{name}"), sort_keys=True)
            for tt, dotted in (targets or {}).items()
        }
        if len(types) != 1:
            fail(f"{where}: the attributes it fills have {len(types)} types; one input has one")
        shape.shared[str(name)] = (json.loads(types.pop()), sorted(map(str, targets)))
    return shape


def with_defaults(shape: Shape, schemas: dict[str, Any]) -> dict[str, Any]:
    """The schemas with each attribute the module supplies made optional.

    The variable is derived from the schema, so an attribute the module fills
    has to be one an entry may omit, at whatever depth it sits.
    """
    relaxed = copy.deepcopy(schemas)
    for terraform_type, slots in shape.defaults.items():
        for path in slots:
            attributes = _attributes(relaxed, terraform_type)
            for part in path[:-1]:
                attributes = attributes[part]["nested_type"]["attributes"]
            attributes[path[-1]].update(required=False, optional=True)
    return relaxed


def _value(
    own: str,
    slot: str | dict[str, Any],
    prefix: tuple[str, ...],
    lists: set[tuple[str, ...]],
) -> str:
    if isinstance(slot, str):
        return f"{own} != null ? {own} : ({slot})"
    if prefix in lists:
        item = f"item{len(prefix)}"
        inner = ", ".join(
            f"{key} = {_value(f'{item}.{key}', sub, (*prefix, key), lists)}" for key, sub in sorted(slot.items())
        )
        # An element may be null as well as the collection, and neither has
        # attributes to read.
        return f"{own} == null ? null : [for {item} in {own} : {item} == null ? null : merge({item}, {{ {inner} }})]"
    inner = ", ".join(
        f"{key} = {_value(f'{own}.{key}', sub, (*prefix, key), lists)}" for key, sub in sorted(slot.items())
    )
    # An entry may leave the object out, and merge() refuses a null. A default
    # inside an object the entry omits is not applied: writing it would create
    # an object the caller did not ask for, and change the request.
    return f"{own} == null ? null : merge({own}, {{ {inner} }})"


def entry_value(shape: Shape, terraform_type: str, attribute: str) -> str:
    """The expression a resource argument reads its value from."""
    tree: dict[str, Any] = {}
    for path, default in (shape.defaults.get(terraform_type) or {}).items():
        if path[0] != attribute:
            continue
        node = tree
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = default
    own = f"each.value.{attribute}"
    lists = {prefix for tt, prefix in shape.lists if tt == terraform_type}
    return _value(own, tree[attribute], (attribute,), lists) if tree else own


def module_variables(shape: Shape, hcl_type: Callable[[Any], str]) -> list[str]:
    out: list[str] = []
    if shape.named:
        out += [
            f'variable "{PREFIX}" {{',
            '  description = "Names what this module creates <prefix>-<key>, where an entry '
            'sets no name. Unset, the name is the key."',
            "  type        = string",
            "  default     = null",
            "}",
            "",
        ]
    for name, (json_type, filled) in sorted(shape.shared.items()):
        out += [
            f'variable "{name}" {{',
            f'  description = "The default for each entry of {", ".join(filled)} that sets none."',
            f"  type        = {hcl_type(json_type)}",
            "  default     = null",
            "}",
            "",
        ]
    return out
