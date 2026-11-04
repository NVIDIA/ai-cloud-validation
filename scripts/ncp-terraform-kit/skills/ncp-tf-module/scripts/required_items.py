# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
"""Which fields an item of a nested input must hold, from the provider's reviewed schemas.

Inside an optional and computed value, the provider marks every field optional
and computed, even one that the API requires: Terraform keeps what the read
filled only when no field inside is required. So the export cannot say which
fields an item needs. A module input has no read. A caller that writes an item
must write each field that the API requires in it, or the API refuses the item.
The reviewed provider schema keeps that fact as each attribute's `behaviour`.

A top-level attribute keeps the export's flags: there the provider already
marks Required each field that the API requires.

Pure: plain data in, plain data out.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from typing import Any

Path = tuple[str, ...]


def required_paths(documents: Iterable[dict[str, Any]]) -> dict[str, set[Path]]:
    """{terraform_type: the nested paths whose `behaviour` is required}, for every
    resource that the provider schema documents hold."""
    found: dict[str, set[Path]] = {}

    def walk(attributes: list[dict[str, Any]], path: Path, into: set[Path]) -> None:
        for attribute in attributes:
            here = (*path, attribute["name"])
            if path and attribute.get("behaviour") == "required":
                into.add(here)
            walk(attribute.get("attributes") or [], here, into)

    for document in documents:
        for resource in document.get("resources", []):
            walk(resource.get("attributes", []), (), found.setdefault(resource["terraform_type"], set()))
    return found


def with_required_items(
    schemas: dict[str, Any],
    types: Iterable[str],
    paths: dict[str, set[Path]],
    fail: Callable[[str], None],
) -> dict[str, Any]:
    """The export's resource schemas, with each field that the API requires in an
    item of a settable nested value marked required. Refuses a type that no
    provider schema holds."""
    missing = sorted(t for t in types if t not in paths)
    if missing:
        fail(f"no provider schema holds {missing}: pass each provider schema file that the provider step wrote")
    marked = copy.deepcopy(schemas)
    for terraform_type in types:
        attributes = marked[terraform_type].get("block", {}).get("attributes", {})
        for path in paths[terraform_type]:
            _mark(attributes, path, path, terraform_type, fail)
    return marked


def _mark(
    attributes: dict[str, Any], path: Path, whole: Path, terraform_type: str, fail: Callable[[str], None]
) -> None:
    head, *rest = path
    attribute = attributes.get(head)
    if attribute is None:
        # Skipping it would let the module accept a value that the API refuses.
        fail(f"{terraform_type}: the reviewed schema requires {'.'.join(whole)}, which the schema export does not hold")
        return
    if rest:
        _mark(attribute.get("nested_type", {}).get("attributes", {}), tuple(rest), whole, terraform_type, fail)
    elif attribute.get("optional"):
        attribute.update(required=True, optional=False, computed=False)
