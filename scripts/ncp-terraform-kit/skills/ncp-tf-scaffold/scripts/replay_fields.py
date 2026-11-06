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
"""The fields that a recorded read returns and the provider's schema drops.

A cassette holds what the API returned once. A field in a recorded read that
no attribute of the schema export holds never reaches state, and no replay step
fails on it, because the provider reads only the attributes it declares. The
render reports each one, so the operator can add it to the schema or record why
the provider leaves it out.

Pure: plain data in, plain data out.
"""

from __future__ import annotations

import json
from typing import Any

#: A shape: each attribute's name, as `_key` writes it, with the shape inside
#: it, or None for a value the export does not describe field by field.
Shape = dict[str, Any]


def _key(name: str) -> str:
    """One spelling for the API's name and the attribute's: `gatewayIPv4` and
    `gateway_ipv4` name one field, and the export holds only the second."""
    return name.replace("_", "").replace("-", "").lower()


def _type_shape(kind: Any) -> Shape | None:
    """The shape of a Terraform type: an object's attributes, through a list or
    a set of objects. A map's keys are values, not fields."""
    if isinstance(kind, list) and len(kind) == 2:
        if kind[0] == "object" and isinstance(kind[1], dict):
            return {_key(name): _type_shape(inner) for name, inner in kind[1].items()}
        if kind[0] in ("list", "set"):
            return _type_shape(kind[1])
    return None


def shape(block: dict[str, Any]) -> Shape:
    """The fields that a resource's block in the schema export holds, at every depth."""
    found: Shape = {}
    for name, attribute in (block.get("attributes") or {}).items():
        attribute = attribute or {}
        nested = attribute.get("nested_type")
        found[_key(name)] = shape(nested) if nested else _type_shape(attribute.get("type"))
    for name, nested in (block.get("block_types") or {}).items():
        found[_key(name)] = shape((nested or {}).get("block") or {})
    return found


def _objects(block: dict[str, Any]) -> dict[str, Shape]:
    """Each top-level attribute that holds one object, with its shape."""
    found: dict[str, Shape] = {}
    for name, attribute in (block.get("attributes") or {}).items():
        attribute = attribute or {}
        nested = attribute.get("nested_type") or {}
        kind = attribute.get("type")
        if nested.get("nesting_mode") == "single":
            found[_key(name)] = shape(nested)
        elif isinstance(kind, list) and kind[:1] == ["object"]:
            found[_key(name)] = _type_shape(kind) or {}
    return found


def _wrapper(block: dict[str, Any], names: list[str]) -> Shape:
    """The shape of the attribute that the resource's fields sit under, or none.

    A create that nests its payload under one field makes the provider keep the
    read's answer under that attribute, so a field the block lacks at its top
    is held there. It is the one top-level object that holds such a field; when
    none or several do, no field is held under one.
    """
    holding = [inner for inner in _objects(block).values() if any(_key(n) in inner for n in names)]
    return holding[0] if len(holding) == 1 else {}


def _resources(document: Any, fields: Shape) -> list[dict[str, Any]]:
    """The resource objects in a read's answer.

    The rule the provider's derivation applies: an answer of exactly one field
    that no attribute holds is an envelope, and the resource is inside it. A
    list inside it is a read that lists the resource, item by item.
    """
    if not isinstance(document, dict):
        return []
    if len(document) == 1:
        ((name, inner),) = document.items()
        if _key(name) not in fields:
            if isinstance(inner, dict):
                return [inner]
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    return [document]


def dropped(body: str, block: dict[str, Any]) -> list[str]:
    """Each field path of a recorded read's answer that the block does not
    hold, in the API's spelling. A field that is dropped is named, and not the
    fields inside it."""
    try:
        document = json.loads(body)
    except ValueError:
        return []
    fields = shape(block)
    found: set[str] = set()

    def walk(node: dict[str, Any], held: Shape, prefix: str) -> None:
        for name, value in node.items():
            path = f"{prefix}{name}"
            if _key(name) not in held:
                found.add(path)
                continue
            inner = held[_key(name)]
            if inner is None:
                continue
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict):
                    walk(item, inner, path + ".")

    for resource in _resources(document, fields):
        top = {name: value for name, value in resource.items() if _key(name) in fields}
        walk(top, fields, "")
        rest = {name: value for name, value in resource.items() if name not in top}
        walk(rest, _wrapper(block, list(rest)), "")
    return sorted(found)


def unkept(case_file: str, recorded: dict[str, dict[str, Any]], block: dict[str, Any]) -> list[str]:
    """One line for a case whose recorded reads return a field the block lacks.
    An empty block is a type the export does not describe: nothing to compare."""
    if not block:
        return []
    paths: set[str] = set()
    for interaction in (recorded.get("read") or {}).get("interactions") or []:
        response = (interaction or {}).get("response") or {}
        if 200 <= int(response.get("status") or 0) < 300:
            paths.update(dropped(str(response.get("body") or ""), block))
    if not paths:
        return []
    count = f"{len(paths)} field" + ("s" if len(paths) > 1 else "")
    it = "them" if len(paths) > 1 else "it"
    return [
        f"{case_file}: the recorded read returns {count} that the provider's schema does not hold, "
        f"so no state keeps {it} -- {', '.join(sorted(paths))}"
    ]
