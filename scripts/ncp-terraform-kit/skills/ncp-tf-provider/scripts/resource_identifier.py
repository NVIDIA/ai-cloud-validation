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
"""Where the provider finds the value that addresses one resource.

The read, update and delete each take it in their path. The provider stores it
after the create, so the derivation records where the create gives it:

- `operation`: the create answers with an operation resource, and the runtime
  takes the identifier from the resource that the operation names.
- `attribute`: an attribute carries the parameter's name.
- `response`: the create answers with the read's own schema, the resource.

When none holds, the source is empty, and the record lists the fields of the
create's answer as candidates. Which of them holds the identifier is a reading
of the document, so a person answers it, with `source` `review`.

Pure: plain data in, plain data out.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: The source a person writes when the documents do not say.
REVIEW = "review"

#: The planned verbs whose path names one resource, in the order they are asked.
ADDRESSING = ("read", "delete", "update")

#: A placeholder in a path template: `{id}`, or `{+name}` for a value that
#: spans several segments.
PLACEHOLDER = re.compile(r"\{\+?([^{}]+)\}")


def parameter_of(method: Any, provider_value: Callable[[Any], str]) -> str:
    """The last path placeholder that the provider does not fill, or "".

    The last one names this resource; those before it name its parents.
    """
    filled = {p.name for p in method.parameters if provider_value(p)}
    names = [n for n in PLACEHOLDER.findall(method.template or method.path) if n not in filled]
    return names[-1] if names else ""


def makes_the_account_kind(create: Any, read: Any, provider_value: Callable[[Any], str]) -> bool:
    """Whether the create makes a new object of the kind the account scope names.

    The read's path holds only values that the provider configuration fills,
    the create's path holds none, and the create sends the schema that the read
    answers. The create then makes another object of the account's kind, and
    the read must address that object, not the configured account.
    """
    if create is None or read is None or parameter_of(read, provider_value):
        return False
    if not any(provider_value(p) for p in read.parameters if p.location == "path"):
        return False
    if any(provider_value(p) for p in create.parameters):
        return False
    return bool(create.request_ref) and create.document == read.document and (create.request_ref == read.response_ref)


def addressed_by(operations: dict[str, Any], provider_value: Callable[[Any], str]) -> tuple[str, Any]:
    """The first planned operation that addresses one resource, and its parameter."""
    for verb in ADDRESSING:
        method = operations.get(verb)
        if method is not None and parameter_of(method, provider_value):
            return parameter_of(method, provider_value), method
    return "", None


def addressing_parameters(
    operations: dict[str, Any], create: Any, provider_value: Callable[[Any], str]
) -> tuple[str, ...]:
    """Every path parameter that addresses one resource.

    Most addressing paths end in the object's own parameter. Some take two
    that the create's path does not hold, as Cloud DNS addresses a record set
    by `.../rrsets/{name}/{type}`: each of them names the object, and a new
    value of either is another object.
    """
    parameter, method = addressed_by(operations, provider_value)
    if not parameter:
        return ()
    filled = {p.name for p in method.parameters if provider_value(p)}
    held = set(PLACEHOLDER.findall(create.template or create.path)) if create is not None else set()
    names = [n for n in PLACEHOLDER.findall(method.template or method.path) if n not in filled | held]
    return tuple(names) if len(names) > 1 else (parameter,)


def _names_an_earlier_object(create: Any, method: Any) -> bool:
    """Whether the create's own path takes the addressed object, on another path.

    A create on the addressing path, such as a PUT of a caller-chosen id,
    creates the object that the path names. A create on another path that
    takes it, such as `.../firewallPolicies/{firewallPolicy}/addRule`, needs
    that object to exist first.

    The flat path is read, not the template: Discovery writes `{+name}` for the
    project in a create and for the service account in its read, and the flat
    path names them `{projectsId}` and `{serviceAccountsId}`.
    """
    addressed = PLACEHOLDER.findall(method.path)
    if not addressed or addressed[-1] not in PLACEHOLDER.findall(create.path):
        return False
    return PLACEHOLDER.sub("*", create.path) != PLACEHOLDER.sub("*", method.path)


def for_resource(
    operations: dict[str, Any],
    create: Any,
    attribute_names: set[str],
    answer: list[Any],
    provider_value: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str]]:
    """The identifier record of one resource, and a note when it is open.

    `operations` maps each planned verb to its resolved method, or None.
    `answer` is the create's answer, expanded and out of its envelope.
    """
    parameter, method = addressed_by(operations, provider_value)
    if not parameter:
        filled = [
            m.operation_id
            for v in ADDRESSING
            if (m := operations.get(v)) is not None and PLACEHOLDER.search(m.template or m.path)
        ]
        return {"parameter": "", "source": ""}, [
            f"{filled[0]} takes only path parameters that the provider configuration fills, "
            f"so it addresses the configured account rather than this resource"
            if filled
            else "no planned read, delete or update takes a path parameter, so the provider has "
            "nothing to address this resource by"
        ]
    if create is not None and _names_an_earlier_object(create, method):
        return {"parameter": parameter, "source": ""}, [
            f"{create.operation_id} already takes {parameter!r} in its path, so {parameter!r} "
            f"names an object that exists before the create, and this resource is an entry "
            f"inside it. No path parameter addresses the entry itself, so the provider has "
            f"no identifier to store for it"
        ]
    if create is not None and create.response_ref.endswith("Operation"):
        return {"parameter": parameter, "source": "operation"}, []
    if parameter in attribute_names:
        return {"parameter": parameter, "source": "attribute", "field": parameter}, []
    if (
        create is not None
        and create.response_ref
        and create.response_ref == ((operations.get("read") or method).response_ref)
    ):
        return {"parameter": parameter, "source": "response"}, []
    candidates = [{"name": a.name, "type": a.type, "description": a.description} for a in answer]
    return {"parameter": parameter, "source": "", "candidates": candidates}, [
        f"{method.operation_id} takes {parameter!r}, and no document says which field of "
        f"the create's answer holds it: identifier.candidates lists {len(candidates)} "
        f"field(s)"
    ]
