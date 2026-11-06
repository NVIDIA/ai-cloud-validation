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
"""A value that OpenAPI writes as `anyOf` several objects, as one object.

The value is an object of one of the branches, so it can hold the fields of
each. A field that every branch requires is in every value, so it stays
required. A field that only some branches hold or require is absent from a
value of another branch, so it is not required.

Pure: plain data in, plain data out.
"""

from __future__ import annotations

from typing import Any


def merged(prop: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any] | None:
    """The one object that `prop`'s `anyOf` branches describe, or None.

    None when `prop` has no `anyOf`, or when a branch does not resolve to an
    object with fields: a string or a null branch makes the value something
    other than an object, and which one is not the derivation's to choose.
    """
    branches = prop.get("anyOf")
    if not isinstance(branches, list) or not branches:
        return None
    objects = [_object(branch, schemas) for branch in branches]
    if any(o is None for o in objects):
        return None
    properties: dict[str, Any] = {}
    for o in objects:
        for name, field in o["properties"].items():
            properties.setdefault(name, field)
    required = set.intersection(*(set(o.get("required") or ()) for o in objects))
    own = {key: value for key, value in prop.items() if key != "anyOf"}
    return {**own, "type": "object", "required": sorted(required), "properties": properties}


def _object(branch: Any, schemas: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(branch, dict):
        return None
    reference = branch.get("$ref")
    target = schemas.get(str(reference).rsplit("/", 1)[-1]) if reference else branch
    if isinstance(target, dict) and isinstance(target.get("properties"), dict):
        return target
    return None
