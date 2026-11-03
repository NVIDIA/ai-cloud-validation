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
"""Whether an API document marks a field or a method deprecated.

A deprecated field is one that the API is retiring, so the provider gives it
no attribute unless an operator's override keeps it, and the coverage plans no
deprecated call. The provider and the
coverage use this one rule.

Pure: no imports beyond the standard library, so each skill can load it.
"""

from __future__ import annotations

import re
from typing import Any

#: A document without the `deprecated` flag says it in prose: `DEPRECATED.`,
#: `[Deprecated]`, `Deprecated:`. A clause of its own, in the anchored form of
#: the output-only marker, because the word also names a future change ("To be
#: deprecated.", "will be deprecated soon") and a state that a field reports
#: ("The deprecation status of this image"), and neither retires the field.
MARKER = re.compile(r"(?:^|[.;:(]\s*)\[?deprecated\s*(?:[.:)\]]|$)", re.IGNORECASE)

#: The end of the note for each field that the provider leaves out. The
#: coverage report finds the fields by it.
LEFT_OUT = ": the document marks it deprecated, so no attribute exposes it"


def is_deprecated(node: Any) -> bool:
    """Whether a property, a parameter or a method is marked deprecated."""
    if not isinstance(node, dict):
        return False
    if node.get("deprecated") is True:
        return True
    return bool(MARKER.search(str(node.get("description") or "").strip()))


def kept(
    properties: dict[str, Any], path: tuple[str, ...], notes: list[str], keep: frozenset[str] = frozenset()
) -> list[tuple[str, Any]]:
    """The properties, sorted by name, without each deprecated one that `keep`
    does not name. A note names each one left out by the schema path that
    holds it."""
    out: list[tuple[str, Any]] = []
    for name, prop in sorted(properties.items()):
        if is_deprecated(prop) and name not in keep:
            notes.append(f"{'.'.join((*path, name))}{LEFT_OUT}")
        else:
            out.append((name, prop))
    return out


def left_out(notes: list[str]) -> list[str]:
    """The schema path of each field that a note says the provider leaves out."""
    return [note[: -len(LEFT_OUT)] for note in notes if note.endswith(LEFT_OUT)]
