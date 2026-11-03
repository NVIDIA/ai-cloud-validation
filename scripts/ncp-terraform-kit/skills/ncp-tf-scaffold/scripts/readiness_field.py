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
"""Whether a case's readiness names a field of the resource itself.

The live test reads the resource and holds the readiness field to its value. A
field that only the create's long-running operation returns, such as its
`done`, is never in that read, so the check fails on a resource that is ready.
The provider's schema export lists the resource's attributes, so a field that
none of them holds is refused before anything renders. Which state means
usable is the operator's call, so the refusal asks for it and suggests none.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import case_identity
import fixture_values
import lifecycle_checks

#: A field name the live walker matches with case, underscores and hyphens
#: ignored. Any other name it matches only exactly, as the read spells it.
LOOSE_NAME = re.compile(r"[a-z0-9_]+")


def _same(field: str, attribute: str) -> bool:
    """Whether the read's field is the attribute, by the live walker's rule.

    The read spells a field as its document does, in lowerCamelCase, and the
    export spells the attribute in snake_case.
    """
    if LOOSE_NAME.fullmatch(field):
        return field.replace("_", "") == attribute.lower().replace("_", "").replace("-", "")
    head, *rest = attribute.split("_")
    return field == head + "".join(part.capitalize() for part in rest)


def _holds(attributes: dict[str, Any], segments: list[str]) -> bool | None:
    """Whether the attributes hold the dotted field: True, False, or None when
    the export does not describe the level the field reaches."""
    found = [value for key, value in attributes.items() if _same(segments[0], key)]
    if not found:
        return False
    if len(segments) == 1:
        return True
    nested = ((found[0] or {}).get("nested_type") or {}).get("attributes")
    return _holds(nested, segments[1:]) if nested else None


def _compared(case: dict[str, Any], export: dict[str, Any]) -> bool | None:
    """Whether the resource has the readiness field, or None when the export
    cannot say.

    The read returns the resource itself. Its attributes hold that object at
    the top, or under the argument that wraps the whole resource, as a node
    pool's attributes hold its `status` under `node_pool`.
    """
    named = lifecycle_checks.readiness(case)
    terraform_type = case_identity.terraform_type(case)
    attributes = fixture_values.resource_block(export, terraform_type).get("attributes") or {}
    if not named or not attributes:
        return None
    segments = named[0].split(".")
    held = _holds(attributes, segments)
    wrapper = fixture_values.wrapper((case.get("fixture") or {}).get("create") or {})
    if held is False and wrapper in attributes:
        nested = ((attributes[wrapper] or {}).get("nested_type") or {}).get("attributes")
        held = _holds(nested, segments) if nested else None
    return held


def refusals(case: dict[str, Any], export: dict[str, Any]) -> list[str]:
    """The refusal of a readiness whose field the resource does not have, or nothing."""
    if case.get("contract") != "resource" or _compared(case, export) is not False:
        return []
    named = lifecycle_checks.readiness(case) or ("", "")
    terraform_type = case_identity.terraform_type(case)
    return [
        f"readiness names `{named[0]}`, and {terraform_type} has no attribute "
        f"`{named[0]}` in the provider's schema export: the resource's read does "
        "not return it, so the live check would fail on a resource that is ready. "
        "A field of the create's operation, such as `done`, is not the resource's. "
        "Which state means usable is the operator's call: ask the operator for it, "
        "and do not choose one"
    ]


def note_unchecked(found: list[tuple[Path, dict[str, Any]]], export: dict[str, Any]) -> None:
    """Say which readiness fields the run did not compare, on stderr.

    A comparison that skips itself in silence reads as one that passed.
    """
    named = [
        (path, case) for path, case in found if case.get("contract") == "resource" and lifecycle_checks.readiness(case)
    ]
    if named and not export:
        print(
            "NOTE  readiness fields not compared with the resource's attributes; "
            "pass --provider-export, the provider's provider-schema-export.json",
            file=sys.stderr,
        )
        return
    for path, case in named:
        if _compared(case, export) is None:
            print(
                f"NOTE  {path}: the provider's schema export does not describe the "
                f"readiness field of {case_identity.terraform_type(case)}; it is not compared",
                file=sys.stderr,
            )
