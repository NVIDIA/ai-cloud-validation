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
"""A create path parameter that names the scope, spelled as the object's name.

A create path such as `v3/{+name}/alertPolicies` appends a collection to the
parameter, so the parameter names the scope that the object goes into. A read
path such as `v3/{+name}` ends with the parameter, so there the parameter
names the object itself. When both carry one name, the create's parameter and
the body field of that name hold two different values: the scope, and the
object's own name. The body field must not take the parameter's `required`.

Pure: no imports beyond the standard library.
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"^\{\+?([^{}]+)\}$")


def _segments(template: str) -> list[str]:
    return [s for s in template.split("/") if s]


def _scope_parameters(template: str) -> set[str]:
    """Each parameter that a further segment of the path follows."""
    segments = _segments(template)
    found: set[str] = set()
    for segment in segments[:-1]:
        match = _PLACEHOLDER.match(segment)
        if match:
            found.add(match.group(1))
    return found


def _last_parameter(template: str) -> str:
    """The parameter that the path ends with, or "" when it ends with a literal."""
    segments = _segments(template)
    match = _PLACEHOLDER.match(segments[-1]) if segments else None
    return match.group(1) if match else ""


def named_like_the_object(create: Any, read: Any | None) -> frozenset[str]:
    """Each create path parameter that names the scope, while the read path
    names the object with a parameter of the same name.

    Without a read, nothing says which value the body field holds, so the
    result is empty.
    """
    if read is None:
        return frozenset()
    own = _last_parameter(read.template or read.path)
    scope = _scope_parameters(create.template or create.path)
    path = {p.name for p in create.parameters if p.location == "path"}
    return frozenset({own} & scope & path)


def unfilled(operation_id: str, names: set[str] | frozenset[str]) -> list[str]:
    """A note for each scope parameter that the body's own field hides and
    that the provider configuration does not fill.

    One attribute cannot hold the scope and the object's name, and the
    documents give the scope no other name. A name that the kit chose would
    be a guess, so the schema declares none and the note asks for one.
    """
    return [
        f"{operation_id} takes the scope of the object in the path parameter `{name}`, and "
        f"its body field `{name}` is the object's own name, as the read path shows. No "
        f"attribute fills the parameter. Name the scope attribute and record the decision "
        f"for review"
        for name in sorted(names)
    ]
