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
"""Replacements that an unmanaged method could avoid.

A planned field that no planned call changes replaces its resource on a
change. When a method of the object's own carries that field, and the plan
leaves the method unmanaged, the API may change the field in place, so the
replacement is a choice that costs data or downtime: the operator approves it
by name.

The match is narrow, because a wrong line here asks the operator about nothing.
A method counts only when its request is a request wrapper, not an object, and
the wrapper carries the field, or `destination` and the field's name. A field
also counts when its own description names the method with the object's kind,
as "MoveFolder".
"""

from __future__ import annotations

import re
from typing import Any

import operation_refs
from api_spec import Operation, Spec

DESTINATION = "destination"
WRAPPER = "Request"


def _changed(replacing: set[str], operation: Operation, method: str, create: Operation | None) -> list[str]:
    """The replacing fields that `operation` carries."""
    found = []
    if operation.request_schema.endswith(WRAPPER):
        for field in operation.request_fields:
            if field.startswith(DESTINATION) and field[len(DESTINATION) :][:1].isupper():
                field = field[len(DESTINATION)].lower() + field[len(DESTINATION) + 1 :]
            if field in replacing:
                found.append(field)
    if create is not None and create.request_schema:
        named = re.compile(rf"\b{re.escape(method[:1].upper() + method[1:] + create.request_schema)}\b")
        found += [field for field, text in create.request_fields.items() if field in replacing and named.search(text)]
    return list(dict.fromkeys(found))


def lines(
    classified: list[tuple[str, dict[str, Any], str, list[tuple[Operation, str]]]],
    replaced: list[Any],
    spec: Spec,
    method_name: Any,
) -> list[str]:
    """The decision lines: each replacing field that an unmanaged method of the
    same object carries, with the method."""
    by_type = {item.terraform_type: {path.split(".")[0] for path in item.fields} for item in replaced}
    rows = []
    for _, resource, item, operations in classified:
        replacing = by_type.get(str(resource.get("terraform_type")), set())
        if not replacing:
            continue
        create = operation_refs.resolve(str((resource.get("operations") or {}).get("create") or ""), spec)
        for operation, place in operations:
            if place not in ("unmanaged_methods", "module_plan.unmanaged_methods"):
                continue
            fields = _changed(replacing, operation, method_name(operation, item), create)
            if fields:
                rows.append(
                    f"| {resource.get('terraform_type')} | {', '.join(f'`{f}`' for f in fields)} "
                    f"| {operation_refs.cited(operation)} |"
                )
    if not rows:
        return []
    return [
        "- A change of these fields replaces the resource, and a method of the same object that the "
        "plan does not manage carries the field, so it may change it in place. Read each method: "
        "approve the replacement, or ask for the method as an update call.",
        "",
        "| type | field | unmanaged method that carries it |",
        "|---|---|---|",
        *rows,
    ]
