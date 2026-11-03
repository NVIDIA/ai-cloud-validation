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

"""The API object behind each planned type.

An NCP with no Terraform provider has no names to follow but its API
documents, and its operators find the API object behind each type. The report
shows that object beside each type, so a review sees a name that tells one
use or one part of the object. `shared-create` refuses two planned types with
one create: two types over one object hide it twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import operation_refs
from api_spec import Spec

#: The planned verbs whose path names the object, in the order they are read.
ADDRESSING = ("read", "delete", "update")


def api_object(operations: dict[str, Any], spec: Spec) -> str:
    """The object a resource manages, as its document names it, or "".

    A dotted identifier names its collection, as `compute.resourcePolicies`. A
    flat document names it by the item path of the read, delete or update, as
    `/v1/instances`, because a create can be an action on another path, as
    `/v1/instance-operations/launch`.
    """
    create = operation_refs.resolve(str(operations.get("create") or ""), spec)
    if create is None:
        return ""
    if "." in create.operation_id:
        return create.operation_id.rsplit(".", 1)[0]
    for verb in ADDRESSING:
        operation = operation_refs.resolve(str(operations.get(verb) or ""), spec)
        if operation is not None and operation.normalized_path.endswith(f"/{operation_refs.PARAMETER}"):
            return operation.path.rsplit("/", 1)[0]
    return create.path


def report_lines(module_plan: dict[str, Any], spec: Spec | None) -> list[str]:
    """The report's table of planned types and the API object of each."""
    lines = [
        "",
        "## Planned types and their API objects",
        "",
        "Each type should name its API object, not one use or one part of it.",
        "",
        "| layer | type | API object | create |",
        "|---|---|---|---|",
    ]
    for layer, layer_plan in (module_plan.get("layers") or {}).items():
        for resource in layer_plan.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            operations = resource.get("operations") or {}
            target = api_object(operations, spec) if spec is not None else ""
            lines.append(
                f"| {layer} | {resource.get('terraform_type', '?')} | "
                f"{target or '?'} | {operations.get('create', '')} |"
            )
    return lines


def shared_create_failures(planned: Iterable[tuple[str, dict[str, Any]]], spec: Spec) -> list[str]:
    """One failure for each create operation that two or more planned types name."""
    owners: dict[Any, list[str]] = {}
    spelled: dict[Any, str] = {}
    for where, resource in planned:
        create = str((resource.get("operations") or {}).get("create") or "")
        if not create:
            continue
        key = operation_refs.identity(create, spec)
        owners.setdefault(key, []).append(where)
        spelled.setdefault(key, create)
    return [
        f"{spelled[key]} is the create of {', '.join(wheres)}. One API object is one type: "
        f"name the object once, after the object and not after one use of it"
        for key, wheres in owners.items()
        if len(wheres) > 1
    ]
