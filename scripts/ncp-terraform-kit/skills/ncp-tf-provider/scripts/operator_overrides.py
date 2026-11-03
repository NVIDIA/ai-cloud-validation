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

"""The operator's decision on a field, applied after the derivation.

A plan's `field_overrides` state what no condition can: a change of a field
replaces the object, changes it in place, or the provider leaves the field out.
The derivation reads the documents; the operator's word comes after it and
wins. Each attribute it decides says so, so a later step does not take the
decision for a reading of the documents.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import replacement

#: Who decided an attribute that an override names.
OPERATOR = "operator"


def apply(attributes: Iterable[Any], resource: dict[str, Any], wrapper: str = "") -> tuple[list[Any], list[str]]:
    """The attributes with each override applied, and a note for each one.
    A field that the create nests under `wrapper` is found under it too."""
    out = list(attributes)
    notes: list[str] = []
    for entry in (e for e in resource.get("field_overrides") or [] if isinstance(e, dict)):
        field, decision = str(entry.get("field") or ""), str(entry.get("decision") or "")
        path = field.split(".")
        if not any(a.name == path[0] for a in out) and wrapper:
            path = [wrapper, *path]
        out, applied = _at(out, path, decision)
        reason = str(entry.get("reason") or "")
        if not applied:
            notes.append(
                f"field_overrides: `{field}` names no attribute, so the operator's `{decision}` "
                f"is not applied. Correct the field in the plan"
            )
        elif decision == "leave_out":
            notes.append(f"field_overrides: the operator left out `{field}`, so no attribute exposes it: {reason}")
        else:
            notes.append(f"field_overrides: the operator decided that `{field}` is `{decision}`: {reason}")
    return out, notes


def kept_paths(resource: dict[str, Any], wrapper: str = "") -> frozenset[tuple[str, ...]]:
    """The attribute path of each field that an override decides and does
    not leave out, also under `wrapper`. The derivation keeps such a field
    when the document marks it deprecated: the API can still need it."""
    paths = [
        tuple(str(e.get("field") or "").split("."))
        for e in resource.get("field_overrides") or []
        if isinstance(e, dict) and e.get("field") and e.get("decision") != "leave_out"
    ]
    return frozenset([*paths, *((wrapper, *p) for p in paths if wrapper)])


def _at(attributes: list[Any], path: list[str], decision: str) -> tuple[list[Any], bool]:
    """The attributes with the decision applied at `path`, and whether the
    path named one."""
    out: list[Any] = []
    applied = False
    for attribute in attributes:
        if attribute.name != path[0]:
            out.append(attribute)
        elif len(path) > 1:
            children, applied = _at(list(attribute.attributes), path[1:], decision)
            out.append(replace(attribute, attributes=tuple(children)))
        else:
            applied = True
            if decision != "leave_out":
                out.append(_decided(attribute, decision))
    return out, applied


def _decided(attribute: Any, decision: str) -> Any:
    if decision == "replaces":
        marked = attribute if attribute.forces_replacement else replacement._described(attribute)
        return replace(marked, decided_by=OPERATOR)
    text = attribute.description.replace(f" {replacement.SENTENCE}", "").replace(replacement.SENTENCE, "")
    return replace(
        attribute, forces_replacement=False, replaces_when="", description=text.rstrip(), decided_by=OPERATOR
    )
