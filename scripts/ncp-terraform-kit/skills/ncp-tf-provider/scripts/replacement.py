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

"""A field that no planned call can change forces a replacement.

Terraform plans an in-place update for any changed attribute unless the
attribute says that a change replaces the resource. Without that, a change to
a field of a resource with no update, or to a field that its update cannot
send, plans an update that the provider refuses or that the API ignores. The
calls that can change a field are in the plan: the update's request, limited
by a `changes_only` condition, and each update call's fields. A field that
names the object, a path parameter of its create or the parameter that
addresses it, replaces even when an update body echoes it: another value is
another object. So does a field that a `create_only` condition names: its own
description says that it never changes after the create.

Which field a path parameter fills, unless the patterns say it
(`address_pattern`), and whether an optional parameter of the create names the
object, a description alone can say. The derivation asks it in a `naming`
entry, and a field that the review's answer names is a field that names the
object.

When the documents cannot prove that no call changes a field, the field is not
marked: it is listed for the agent. A wrong replacement destroys the object
and its data; a missed one fails at apply, where the operator sees it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

#: The sentence the generated docs show for a field that replaces, as the
#: official providers write it.
SENTENCE = "Changing this forces a new resource to be created."

SETTABLE = ("required", "optional")

#: Where a decision comes from, as the coverage report labels it. `DOCUMENT`
#: is a sentence that the plan quotes: a `create_only` field, or a field that
#: a `changes_only` update leaves out. `ASSUMED` is a field that the update
#: changes in place only because its body carries the field: no sentence and
#: no update call says that the field can change.
DOCUMENT = "document"
ASSUMED = "assumed"


def _settable(attribute: Any) -> bool:
    return any(flag in SETTABLE for flag in attribute.terraform_flags)


def _described(attribute: Any) -> Any:
    text = attribute.description.rstrip()
    joined = f"{text} {SENTENCE}" if text else SENTENCE
    return replace(attribute, description=joined, forces_replacement=True)


def mark(
    attributes: Iterable[Any],
    *,
    update: dict[str, Any] | None,
    payload: set[str],
    limited: set[str] | None,
    call_fields: set[str],
    steering: frozenset[str] = frozenset(),
    optional_parameters: frozenset[str] = frozenset(),
    wrapper: str = "",
    identity: frozenset[str] = frozenset(),
    wrapped_identity: frozenset[str] = frozenset(),
    unnamed: tuple[str, ...] = (),
    fixed: frozenset[str] = frozenset(),
    excluded: frozenset[str] = frozenset(),
    sources: dict[str, str] | None = None,
) -> tuple[list[Any], list[str], list[str], tuple[str, ...]]:
    """The attributes, each one that no call can change marked; a note on the
    ones that are left to the agent; their names, which the coverage report
    shows beside the marks; and each parameter that may name the object
    through a field that only a review can tell.

    `update` is the update's described operation, or None. `payload` is the
    fields its request changes, `limited` the fields a `changes_only` condition
    says it changes, or None, and `call_fields` the fields of every update
    call, each one a field or a path into one such as `metadata.items`.
    `steering` names the fields and parameters that guard or shape one request
    rather than describe the object, and `optional_parameters` the attributes
    that come from an optional parameter of the create. `wrapper` is the field
    the create nests the object under: the object's fields are its children,
    and the wrapper itself is never marked. `identity` names the top-level
    attributes that name the object and `wrapped_identity` the wrapped ones,
    and `unnamed` each path parameter that addresses the object and that no
    attribute is known to fill. `fixed` names the fields that a `create_only`
    condition says never change after the create, each one a field or a path
    to a field nested in one, such as `versionTemplate.protectionLevel`; a path
    marks that nested field alone, and its parent stays as the calls leave it.
    `excluded` names the fields that a `cannot_change` condition says the
    update cannot change, though its request carries them.
    `sources`, when given, receives the `DOCUMENT` or `ASSUMED` source of each
    such field, by its path; a field it does not name is derived from the
    request shapes.
    """
    allowed = (payload if limited is None else payload & limited) - excluded
    called = {field for field in call_fields if "." not in field}
    whole = called | allowed
    partly = _paths(field for field in call_fields if field.split(".", 1)[0] not in whole)
    off_path = set((update or {}).get("request_fields_off_path") or [])
    if limited is not None:
        off_path &= limited
    nested = _paths(field for field in fixed if "." in field)
    found_nested = {field: DOCUMENT for field in fixed if "." in field}
    fixed = frozenset(field for field in fixed if "." not in field)
    undecided: list[str] = []
    parameters: list[str] = []
    found = sources if sources is not None else {}
    found.update(found_nested)

    def decide(attribute: Any, names: frozenset[str], prefix: str = "") -> Any:
        path = f"{prefix}{attribute.name}"
        if not _settable(attribute) or attribute.name in steering:
            return attribute
        if attribute.name in fixed:
            found[path] = DOCUMENT
            return _described(attribute)
        if attribute.name in names and attribute.name not in called and attribute.name not in partly:
            return _described(attribute)
        if attribute.name in whole:
            if attribute.name not in called:
                found[path] = ASSUMED if limited is None else DOCUMENT
            return attribute
        if attribute.name in partly:
            return _within(attribute, partly[attribute.name])
        if attribute.name in optional_parameters:
            parameters.append(attribute.name)
            return attribute
        if off_path:
            undecided.append(attribute.name)
            return attribute
        if attribute.name in excluded or (limited is not None and attribute.name in payload):
            found[path] = DOCUMENT
        return _described(attribute)

    out: list[Any] = []
    for attribute in attributes:
        if wrapper and attribute.name == wrapper and attribute.attributes:
            decided = replace(
                attribute,
                attributes=tuple(decide(child, wrapped_identity, f"{wrapper}.") for child in attribute.attributes),
            )
        else:
            decided = decide(attribute, identity)
        out.append(_fixed_within(decided, nested[attribute.name]) if attribute.name in nested else decided)
    # When every field replaces, the field that names the object does too.
    if not any(_settable(a) and not a.forces_replacement and a.name not in steering for a in out):
        unnamed = ()
    named = [f"the field that fills {{{parameter}}}" for parameter in unnamed]
    operation = str((update or {}).get("operation") or "?")
    notes = _notes(operation, sorted(off_path), undecided)
    return out, notes, undecided + parameters + named, (*unnamed, *parameters)


def _notes(operation: str, off_path: list[str], undecided: list[str]) -> list[str]:
    """What the agent decides about the fields that a request at another path may change."""
    notes: list[str] = []
    if undecided:
        notes.append(
            f"update: {operation} sends {', '.join(off_path)} at another path, so {', '.join(undecided)} is "
            f"not marked forces_replacement. Which of them such a field changes is your reading: record it "
            f"with its request path, and give each other one the RequiresReplace plan modifier, with the "
            f"reason in your answer"
        )
    return notes


#: The identifier sources under which a field of the request may hold the
#: object's name: the create answered with an operation, or with the object
#: itself. A create that answers with candidate id fields assigned the id.
CALLER_NAMED = ("operation", "response")


def naming(
    create: Any,
    identifier: dict[str, Any],
    top: set[str],
    wrapped: set[str],
    provider_value: Callable[[Any], str],
    addressing: tuple[str, ...] = (),
) -> tuple[frozenset[str], frozenset[str], tuple[str, ...]]:
    """The top-level attributes and the wrapped fields that name the object,
    and each parameter that addresses it when no attribute is known to fill it.

    Each required path parameter of the create scopes the object, as a region
    does; it is a top-level attribute, so a wrapped field of the same name is
    another value. Each parameter that addresses the object, the identifier's
    parameter or each of `addressing`, names it when an attribute has its
    name; otherwise, when the create may have sent the name, the field that
    held it is a reading of the documents. A wrapped field with the name of
    an addressing parameter names the object even when a scope parameter of
    the create has that name too, as IAM's `{+name}` holds the project in the
    create and the account in the read: the scope is the top-level attribute,
    and the wrapped field is the object's own.
    """
    scoped = {p.name for p in create.parameters if p.location == "path" and p.required and not provider_value(p)}
    parameter = str(identifier.get("parameter") or "")
    parameters = addressing or ((parameter,) if parameter else ())
    named, inside, unnamed = set(scoped & top), set(), []
    for name in parameters:
        inside |= {name} & wrapped
        if name in scoped:
            continue
        if name in top | wrapped:
            named |= {name} & top
        elif identifier.get("source", "") in CALLER_NAMED:
            unnamed.append(name)
    return frozenset(named), frozenset(inside), tuple(unnamed)


#: The `source` of a `naming` entry that the review answered. An open entry
#: has an empty source, as an open `identifier` does.
REVIEW = "review"


def reviewed_naming(
    answers: dict[str, dict[str, Any]],
    asked: Iterable[str],
    top: set[str],
) -> tuple[frozenset[str], frozenset[str], dict[str, dict[str, Any]], list[str]]:
    """What the review's `naming` answers decide, for the parameters in `asked`.

    Returns the top-level fields that name the object, the parameters that
    name nothing, the answers that apply, and a note for each answer whose
    field is no top-level attribute. A parameter that the derivation no longer
    asks about has no answer here: the documents now decide it.
    """
    applied = {p: answers[p] for p in asked if isinstance(answers.get(p), dict) and answers[p].get("source") == REVIEW}
    named = {str(a.get("field") or "") for a in applied.values()} - {""}
    nothing = frozenset(p for p, a in applied.items() if not a.get("field"))
    notes = [
        f"naming: the review answers {{{p}}} with {a['field']!r}, and no top-level attribute "
        f"has that api_name, so no field is marked for it"
        for p, a in sorted(applied.items())
        if a.get("field") and a["field"] not in top
    ]
    return frozenset(named & top), nothing, applied, notes


def naming_entries(
    candidates: Iterable[str], applied: dict[str, dict[str, Any]], open_: Iterable[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The resource's `naming` entries, in the order of `candidates`, and a
    note that names the open ones."""
    still = set(open_)
    entries = [
        applied[p] if p in applied else {"parameter": p, "source": ""} for p in candidates if p in applied or p in still
    ]
    asked = open_naming(entries)
    if not asked:
        return entries, []
    return entries, [
        f"naming: {', '.join(asked)} may name the object, and no document says through which field. "
        f"Answer each open entry of `naming` in the review: `source` review, `field` the api_name of "
        f'the top-level attribute that names the object, or "" when none does, and `because`'
    ]


def open_naming(entries: Iterable[dict[str, Any]]) -> list[str]:
    """The parameter of each `naming` entry that no review has answered."""
    return [str(e.get("parameter")) for e in entries if e.get("source") != REVIEW]


def steering_parameters(create: Any, others: Iterable[Any]) -> frozenset[str]:
    """The create's query parameters that the update or the delete takes too.
    A request id or a validate-only flag shapes each write; a parameter that
    names the object is the create's alone."""
    taken = {p.name for method in others if method is not None for p in method.parameters}
    return frozenset(p.name for p in create.parameters if p.location == "query" and p.name in taken)


def _paths(fields: Iterable[str]) -> dict[str, set[str]]:
    """Each field that a path names part of, with the rest of each path."""
    found: dict[str, set[str]] = {}
    for field in fields:
        head, rest = field.split(".", 1)
        found.setdefault(head, set()).add(rest)
    return found


def _within(attribute: Any, paths: set[str]) -> Any:
    """The attribute with each nested field that no path names marked. A call
    that changes part of a field, as a resize changes a reservation's count,
    cannot change the rest of it; the framework replaces on a nested field
    too. A field with no nested schema cannot be split, so it stays as is."""
    if not attribute.attributes:
        return attribute
    whole = {path for path in paths if "." not in path}
    deeper = _paths(path for path in paths if "." in path and path.split(".", 1)[0] not in whole)
    children = []
    for child in attribute.attributes:
        if not _settable(child) or child.name in whole:
            children.append(child)
        elif child.name in deeper:
            children.append(_within(child, deeper[child.name]))
        else:
            children.append(_described(child))
    return replace(attribute, attributes=tuple(children))


def _fixed_within(attribute: Any, paths: set[str]) -> Any:
    """The attribute with each nested field that a path names marked: the
    document says that field never changes, though a call changes the rest."""
    whole = {path for path in paths if "." not in path}
    deeper = _paths(path for path in paths if "." in path)
    children = []
    for child in attribute.attributes:
        if child.name in whole and _settable(child) and not child.forces_replacement:
            children.append(_described(child))
        elif child.name in deeper:
            children.append(_fixed_within(child, deeper[child.name]))
        else:
            children.append(child)
    return replace(attribute, attributes=tuple(children))
