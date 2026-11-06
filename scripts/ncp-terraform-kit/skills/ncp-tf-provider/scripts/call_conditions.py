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

"""The plan's conditions, where the runtime reads them.

The plan records what each method's description limits, and the operator
approves each handling with the plan: a call that acts only on a stopped
object, a field that moves one way. The runtime reads facts under `operations`
and never an operation's name, so each condition rides on the entry of the
operation it names: a delete after which the read still returns the object
carries the marker that means gone. A one-way field is a fact about an
attribute, so it rides on the attribute. What destroy does with an object
that the API never deletes has no operation to ride on, so it rides on the
resource.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

#: The keys of a plan's condition that the runtime reads, in this order.
KEYS = ("kind", "handling", "fields", "direction", "quote", "stopped_when", "deleted_when")

#: The kinds that decide a field, not a call: each one is marked on its
#: attribute, so the runtime has nothing to act on.
FIELD_KINDS = ("create_only", "cannot_change")

#: The change a one-way call cannot make, by the direction it allows.
AGAINST = {"increase": "decrease", "decrease": "increase"}


def _conditions(resource: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in resource.get("conditions") or [] if isinstance(entry, dict)]


def attach(
    operations: dict[str, dict[str, Any]],
    resource: dict[str, Any],
    resolve: Callable[[str], Any],
    describe: Callable[[Any], dict[str, Any]],
) -> list[str]:
    """Put each condition on the entry of the operation it names, and return a
    note for each condition whose operation has no entry."""
    notes: list[str] = []
    for condition in _conditions(resource):
        if condition.get("kind") in FIELD_KINDS:
            continue
        reference = str(condition.get("operation") or "")
        method = resolve(reference)
        key = (method.http_method, method.template or method.path) if method else None
        verb = next((v for v, e in operations.items() if (e.get("http_method"), e.get("path")) == key), None)
        if verb is None:
            notes.append(
                f"condition {condition.get('kind')} names {reference}, which is not a planned "
                f"operation of this resource, so the runtime cannot act on it"
            )
            continue
        entry = {k: condition[k] for k in KEYS if k in condition}
        for call in ("stop", "start"):
            if condition.get("handling") == "stop_and_start" and (found := resolve(str(condition.get(call) or ""))):
                entry[call] = describe(found)
        operations[verb].setdefault("conditions", []).append(entry)
    return notes


def create_only(resource: dict[str, Any]) -> frozenset[str]:
    """The fields that a `create_only` condition says never change after the
    create, as each field's own description states."""
    return frozenset(
        str(field) for c in _conditions(resource) if c.get("kind") == "create_only" for field in c.get("fields") or []
    )


def cannot_change(resource: dict[str, Any]) -> frozenset[str]:
    """The fields that a `cannot_change` condition says the update cannot
    change, as the update's own description states."""
    return frozenset(
        str(field) for c in _conditions(resource) if c.get("kind") == "cannot_change" for field in c.get("fields") or []
    )


def field_marks(resource: dict[str, Any]) -> dict[str, frozenset[str]]:
    """What the field kinds give the replacement derivation: the fields that
    never change after the create, and those that the update cannot change."""
    return {"fixed": create_only(resource), "excluded": cannot_change(resource)}


def on_destroy(resource: dict[str, Any]) -> dict[str, Any]:
    """The operator's choice for an object that the API never deletes: what
    terraform destroy does instead of a delete."""
    entry = resource.get("on_destroy")
    if not isinstance(entry, dict):
        return {}
    return {k: entry[k] for k in ("handling", "reason", "quote") if k in entry}


def one_way(attributes: Iterable[Any], resource: dict[str, Any]) -> list[Any]:
    """Each attribute that a `one_way` condition names, given the change that
    its call cannot make; that change, and no other, replaces the resource."""
    against = {
        str(field): AGAINST[str(c.get("direction"))]
        for c in _conditions(resource)
        if c.get("kind") == "one_way" and c.get("direction") in AGAINST
        for field in c.get("fields") or []
    }
    return [
        replace(a, replaces_when=against[a.name], forces_replacement=False) if a.name in against else a
        for a in attributes
    ]
