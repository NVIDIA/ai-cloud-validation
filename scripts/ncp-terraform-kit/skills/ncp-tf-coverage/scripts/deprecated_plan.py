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

"""The plan sends no call and decides no field that the document marks deprecated.

A deprecated call is one that the API is retiring. The plan records such a
method under `unmanaged_methods`, or such a creation under
`unplanned_operations`, with the reason. The provider gives a deprecated field
no attribute, so a `leave_out` override on one decides nothing. A `replaces` or
`in_place` override keeps the field: a deprecated field can still be one that
the API needs, and only the operator can decide that. The mark is the
provider's own rule, so the plan and the provider cannot disagree.
"""

from __future__ import annotations

import sys
from typing import Any

import api_spec
import operation_refs
import replaced_types
from api_spec import Spec

sys.path.insert(0, str(replaced_types._provider_scripts()))
import deprecated


def _calls(resource: dict[str, Any]) -> list[tuple[str, str]]:
    """Each call the resource sends, with the record that names it."""
    found = [(str(ref), f"its {verb}") for verb, ref in (resource.get("operations") or {}).items()]
    found += [
        (str(call.get("operation") or ""), "an update call")
        for call in resource.get("update_calls") or []
        if isinstance(call, dict)
    ]
    return found + [(str(ref), "an entry method") for ref in resource.get("entry_methods") or []]


def _why(node: dict[str, Any]) -> str:
    if node.get("deprecated") is True:
        return "`deprecated: true`"
    return f'"{" ".join(str(node.get("description") or "").split())}"'


def failures(where: str, resource: dict[str, Any], spec: Spec | None) -> list[str]:
    """What the resource plans that the document marks deprecated."""
    if spec is None:
        return []
    found: list[str] = []
    for reference, record in _calls(resource):
        operation = operation_refs.resolve(reference, spec)
        node = {"deprecated": operation.deprecated, "description": operation.description} if operation else None
        if node and deprecated.is_deprecated(node):
            found.append(
                f"{where}: {reference} is {record}, and the document marks it deprecated ({_why(node)}). "
                f"Plan the call that replaces it, or record the method under `unmanaged_methods`, or a "
                f"creation under `unplanned_operations`, with the reason"
            )
    found += _override_failures(where, resource, spec)
    return found


def _override_failures(where: str, resource: dict[str, Any], spec: Spec) -> list[str]:
    """Each `leave_out` override on a field that the create or the read holds
    and that the document marks deprecated."""
    operations = resource.get("operations") or {}
    create = operation_refs.resolve(str(operations.get("create") or ""), spec)
    read = operation_refs.resolve(str(operations.get("read") or ""), spec)
    bodies = [
        (op.schemas, body)
        for op, body in ((create, create and create.request_body), (read, read and read.response_body))
        if op
    ]
    found: list[str] = []
    for number, entry in enumerate(resource.get("field_overrides") or []):
        field = str(entry.get("field") or "") if isinstance(entry, dict) else ""
        if field and entry.get("decision") != "leave_out":
            continue
        nodes = [api_spec.nested_property(schemas, body, field.split(".")) for schemas, body in bodies] if field else []
        node = next((n for n in nodes if n is not None), None)
        if node is not None and deprecated.is_deprecated(node):
            found.append(
                f"{where}.field_overrides[{number}]: the document marks `{field}` deprecated "
                f"({_why(node)}), so the provider leaves it out. Take the override out. If the API still "
                f"needs the field, the operator keeps it with `replaces` or `in_place` and that reason"
            )
    return found


def report_lines(resources: list[Any]) -> list[str]:
    """The report's table of each field that the provider leaves out because
    the document marks it deprecated."""
    rows = [
        f"| {resource.terraform_type} | {path} |"
        for resource in resources
        for path in dict.fromkeys(deprecated.left_out(resource.notes))
    ]
    if not rows:
        return []
    return [
        "",
        "## Fields the documents mark deprecated",
        "",
        "The provider gives none of these fields an attribute: the document marks each one deprecated, "
        "with the `deprecated` flag or a deprecation marker in its description. Each path names the "
        "schema that holds the field.",
        "",
        "| type | field |",
        "|---|---|",
        *rows,
    ]
