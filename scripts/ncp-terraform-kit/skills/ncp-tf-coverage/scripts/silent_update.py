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

"""`documents_silent.update`: the text that the plan calls limit-free.

That an update states no limit is a reading of its text, and only the agent
can make it. So the plan carries the text that the reading is about: the
update's description, and the description of each schema that a field of its
request names, where a limit is also stated. The gate checks that each text
is the document's text and that none is left out. It does not read what a
text says. The module plan shows the text beside the decision, so the
operator sees each sentence that the plan calls limit-free.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import method_conditions
import operation_refs
from api_spec import Spec

DESCRIPTION = "description"
SCHEMAS = "request_field_schemas"
HEADING = "Updates whose documents state no limit"


def _folded(text: Any) -> str:
    return " ".join(str(text or "").split())


def read(operation: Any) -> dict[str, Any]:
    """The texts that `fields` prints for an update, whitespace folded."""
    schemas = {name: _folded(text) for name, text in method_conditions.field_schemas(operation).items()}
    return {DESCRIPTION: _folded(operation.description), **({SCHEMAS: schemas} if schemas else {})}


def _mismatches(reference: str, label: str, stated: Any, wanted: str | None) -> list[str]:
    if wanted is None:
        return [f"`{label}`: {reference} has no such text. Remove it, or copy the text that `fields` prints"]
    if stated is None:
        return [f"`{label}` is missing. Copy the text that `fields` prints for {reference}"]
    if _folded(stated) != wanted:
        return [
            f"`{label}` is not the text of {reference} in the document. Copy all of the text that "
            f"`fields` prints, word for word"
        ]
    return []


def failures(where: str, reference: str, stated: Any, operation: Any) -> list[str]:
    """What is missing from or different in the silent update's text."""
    shape = (
        f"{where}: `documents_silent.update` holds the text that you read for {reference}: "
        f"`{DESCRIPTION}` and `{SCHEMAS}`, as `fields` prints them"
    )
    if (
        not isinstance(stated, dict)
        or set(stated) - {DESCRIPTION, SCHEMAS}
        or not isinstance(stated.get(SCHEMAS, {}), dict)
    ):
        return [shape]
    wanted = read(operation)
    found = _mismatches(reference, DESCRIPTION, stated.get(DESCRIPTION), wanted[DESCRIPTION])
    have, want = stated.get(SCHEMAS) or {}, wanted.get(SCHEMAS) or {}
    for name in [*want, *(name for name in have if name not in want)]:
        found += _mismatches(reference, f"{SCHEMAS}.{name}", have.get(name), want.get(name))
    return [f"{where}: {failure}" for failure in found]


def report_lines(planned: Iterable[dict[str, Any]], spec: Spec) -> list[str]:
    """Each silent update with its text, for the operator's review."""
    lines: list[str] = []
    for resource in planned:
        stated = (resource.get("documents_silent") or {}).get("update")
        reference = str((resource.get("operations") or {}).get("update") or "")
        if not isinstance(stated, dict) or operation_refs.resolve(reference, spec) is None:
            continue
        lines.append(f"- {resource.get('terraform_type')}: `{reference}`")
        lines.append(f"  - description: {_folded(stated.get(DESCRIPTION)) or '(none)'}")
        lines += [
            f"  - schema of the request field `{name}`: {_folded(text)}"
            for name, text in (stated.get(SCHEMAS) or {}).items()
        ]
    if not lines:
        return []
    return [
        "",
        f"### {HEADING}",
        "",
        "The plan says that each of these updates states no limit, so the update changes in place each "
        "field that its request carries. Below is the text that the plan read for that decision, copied "
        "from the document. Read it. A sentence that limits the update, such as one that names a field "
        "the update cannot change or that allows one field in each request, makes the decision wrong: "
        "record it as a condition in `coverage.yaml`.",
        "",
        *lines,
    ]
