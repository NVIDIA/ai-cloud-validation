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

"""The field that fills a path parameter, when the patterns say which.

A path parameter that addresses the object can state a pattern, and so can a
field of the create's body. When one alternative of the parameter's pattern is
the pattern of exactly one settable top-level field, the path takes that
field's value: compute's `{disk}` accepts `[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?`,
the pattern of the disk's `name`, or a number, the id that the API assigns.
That field names the object, so a new value is another object.

When no field, or more than one field, states such a pattern, the documents
do not say which field fills the parameter, and nothing is derived: the
parameter stays an open `naming` entry for the review.

Pure: plain data in, plain data out.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The anchors that a pattern may carry around the whole value. They do not
#: change which values it accepts here, because both sides are whole values.
_ANCHORS = "^$"


def alternatives(pattern: str) -> set[str]:
    """The top-level alternatives of a regular expression, without anchors."""
    found, depth, start, escaped = [], 0, 0, False
    for index, char in enumerate(pattern):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "|" and depth == 0:
            found.append(pattern[start:index])
            start = index + 1
    found.append(pattern[start:])
    return {part.strip(_ANCHORS) for part in found if part.strip(_ANCHORS)}


def filled_by(
    parameters: Iterable[str], method: Any, properties: dict[str, Any], settable: set[str]
) -> tuple[dict[str, str], list[str]]:
    """Each parameter of `parameters` that one settable field fills, by the
    patterns of `method`'s parameter and of the create body's `properties`,
    and a note for each one that says why."""
    stated = {p.name: p.pattern for p in method.parameters if p.pattern} if method is not None else {}
    found: dict[str, str] = {}
    notes: list[str] = []
    for parameter in parameters:
        accepted = alternatives(stated.get(parameter, ""))
        fields = [
            name
            for name in sorted(settable)
            if str((properties.get(name) or {}).get("pattern") or "").strip(_ANCHORS) in accepted
        ]
        if len(fields) == 1:
            found[parameter] = fields[0]
            notes.append(
                f"naming: {{{parameter}}} of {method.operation_id} accepts the pattern of "
                f"`{fields[0]}`, so `{fields[0]}` names the object"
            )
    return found, notes
