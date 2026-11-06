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
"""What a test calls a dependency resource that it creates, and what the resource gives.

A created dependency resource is named in the field of its reviewed case's
create fixture that holds the kit prefix and the run id: `name` for most
types, `group_key.id` or `account_id` for others. The run id stays at the
front, where the sweeper reads it. The creating case's kind follows it, so two
cases in one run never ask for one name. When the field's limit does not take
the kind, a short tag from a digest of the creating case and the entry stands
for it; when it does not take the rest of the reviewed name either, only the
tag follows the run id.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import attribute_names
from case_names import KIT_PREFIX, NAME_MAX, RUN_ID, TOKEN_ROOM
from dependency_graph import LIMITS

#: The kit prefix and the run id: what the sweeper finds a run's resource by.
HEAD = f"{KIT_PREFIX}-{RUN_ID}"
#: What may follow the head in a name the sweeper reads as the run's.
SEPARATORS = ("-", "@", ".")
#: How many hexadecimal characters of the digest stand for the creating case.
#: Four give 65536 tags, and `render` refuses the rare two cases that share one.
TAG_LENGTH = 4


class NamingError(ValueError):
    """A created name that no form fits, or that two cases share."""


@dataclass(frozen=True)
class Created:
    """A dependency resource as the test creates it."""

    values: dict[str, Any]
    name: str
    #: What `{case}` stands for in the entry's `reference`.
    tag: str
    field: str
    limit: int


def length(name: str) -> int:
    """How long the name is with a run id, as `check` counts a case's own name."""
    return len(name) - len(RUN_ID) + TOKEN_ROOM


def created(setup: dict[str, Any], dependency: dict[str, Any], owner: str, source: str, own: str | None) -> Created:
    """The setup fixture with its run-id field renamed for the creating case.

    `owner` is the creating case's kind, `source` names the creating case and
    the entry for the digest, and `own` is the entry's variable when several
    entries are built from one case.
    """
    path, value = _run_field(setup)
    field = ".".join(str(part) for part in path if not isinstance(part, int))
    limit = _limit(dependency, field)
    tail = value[len(HEAD) :]
    if own:
        tail = "-" + own + ("" if not tail or tail.startswith("-") else tail)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:TAG_LENGTH]
    short_tail = "" if tail.startswith("-") else tail
    forms = [(owner, tail), (digest, tail), (digest, short_tail)]
    for tag, rest in forms:
        name = f"{HEAD}-{tag}{rest}"
        if length(name) <= limit:
            return Created(_with(setup, path, name), name, tag, field, limit)
    shortest = f"{HEAD}-{digest}{short_tail}"
    raise NamingError(
        f"the created {field} {HEAD}-{owner}{tail!s} reaches {length(f'{HEAD}-{owner}{tail}')} "
        f"characters with a run id, and even {shortest} reaches {length(shortest)}; the API "
        f"allows {limit}. Shorten the run-id value in the dependency's own case"
    )


def _run_field(setup: dict[str, Any]) -> tuple[tuple[Any, ...], str]:
    """The one value that names the resource for the run.

    A `name` that carries the run id wins, as before any other field could
    name a created resource, so a name that passes today keeps its form.
    """
    found = list(_run_values(setup, ()))
    named = [(path, value) for path, value in found if path[-1] == "name" and value.startswith(HEAD + "-")]
    if len(named) == 1:
        return named[0]
    if len(found) == 1:
        return found[0]
    raise NamingError(f"has {len(found)} run-id names, want one")


def _run_values(node: Any, path: tuple[Any, ...]) -> Iterator[tuple[tuple[Any, ...], str]]:
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _run_values(child, (*path, key))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _run_values(child, (*path, index))
    elif isinstance(node, str) and (node == HEAD or any(node.startswith(HEAD + s) for s in SEPARATORS)):
        yield path, node


def _limit(dependency: dict[str, Any], field: str) -> int:
    """The field's limit: the name field's, or one the documents state, within the kit's cap."""
    naming = dependency.get("naming") or {}
    named = attribute_names.configuration_path(str(naming.get("name_field") or ""))
    stated = naming.get("name_limit") if field == named else (naming.get(LIMITS) or {}).get(field)
    return min(int(stated or NAME_MAX), NAME_MAX)


def _with(node: Any, path: tuple[Any, ...], value: str) -> Any:
    if not path:
        return value
    if isinstance(node, dict):
        return {key: _with(child, path[1:], value) if key == path[0] else child for key, child in node.items()}
    return [_with(child, path[1:], value) if index == path[0] else child for index, child in enumerate(node)]


# --- across the cases of one render ---------------------------------------------


def shared(per_case: list[tuple[str, list[tuple[dict[str, Any], str]]]]) -> list[str]:
    """Each created dependency resource that two cases would create under one identity.

    Each case comes with its created entries and their names. The identity is
    the entry's reference when it states one, and the created name otherwise:
    two clusters of one name in two locations are two clusters.
    """
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for case_id, entries in per_case:
        for entry, name in entries:
            key = (str(entry["resource"]), str(entry.get("reference") or name))
            here = f"{case_id} ({entry['variable']})"
            if key in seen and not seen[key].startswith(f"{case_id} "):
                clashes.append(
                    f"{seen[key]} and {here} both create {key[0]} {key[1]}; give one of the "
                    "dependency cases another run-id name, or rename one case"
                )
            seen.setdefault(key, here)
    return clashes


# --- an attribute of the created resource ----------------------------------------


def attribute_refusals(
    case: dict[str, Any], export: dict[str, Any], block_of: Callable[[dict[str, Any], str], dict[str, Any]]
) -> list[str]:
    """Each `attribute` path that the provider's schema export does not give as one value.

    A path names an attribute through single nested objects, as the export
    spells them. A list or a set holds several values, so a path through one
    names none of them. A flat path is checked when the export holds the
    dependency's type. Without an export nothing is checked, and
    `unchecked_paths` says so: the scaffold step runs before the provider
    step, so no export exists yet.
    """
    if not export:
        return []
    found = []
    for entry in case.get("dependencies") or []:
        path = str(entry.get("attribute") or "")
        if not path:
            continue
        block = block_of(export, str(entry.get("resource")))
        if "." not in path and not block:
            continue
        if why := _unreadable(block, path.split(".")):
            found.append(
                f"dependency {entry.get('variable')} reads {entry.get('resource')}.{path}: {why}"
                + _elsewhere(block, path.split(".")[-1])
            )
    return found


def unchecked_paths(case: dict[str, Any], export: dict[str, Any]) -> list[str]:
    """Each nested `attribute` path that the run did not compare, for a note.

    A comparison that skips itself in silence reads as one that passed.
    """
    if export:
        return []
    return [
        f"dependency {entry.get('variable')} reads the nested attribute {entry['attribute']}; not compared "
        "with the provider's schema export; pass --provider-export to compare it"
        for entry in case.get("dependencies") or []
        if "." in str(entry.get("attribute") or "")
    ]


def _elsewhere(block: dict[str, Any], name: str) -> str:
    """Where the export gives a value of that name, as a hint for the refusal."""
    paths = sorted(_paths_named(block, name, ()))
    return f"; the export gives {', '.join(paths)}" if paths else ""


def _paths_named(block: dict[str, Any], name: str, prefix: tuple[str, ...]) -> Iterator[str]:
    """Each path through single nested objects to one value called `name`."""
    for key, attribute in (block.get("attributes") or {}).items():
        nested = attribute.get("nested_type") or {}
        if nested.get("nesting_mode") == "single":
            yield from _paths_named(nested, name, (*prefix, key))
        elif not nested and key == name:
            yield ".".join((*prefix, key))
    for key, nested_block in (block.get("block_types") or {}).items():
        if nested_block.get("nesting_mode") == "single":
            yield from _paths_named(nested_block.get("block") or {}, name, (*prefix, key))


def _unreadable(block: dict[str, Any], parts: list[str]) -> str:
    here, label = block, ""
    for index, part in enumerate(parts):
        label = ".".join(parts[: index + 1])
        attribute = (here.get("attributes") or {}).get(part)
        nested_block = (here.get("block_types") or {}).get(part)
        if attribute is None and nested_block is None:
            return f"the export has no {label}"
        last = index == len(parts) - 1
        nested = (attribute or {}).get("nested_type") or (nested_block or {})
        if last:
            return f"{label} is a nested object, not one value" if nested else ""
        if not nested:
            return f"{label} is one value, with nothing inside it"
        if nested.get("nesting_mode") != "single":
            return f"{label} is a {nested.get('nesting_mode')}, which holds several values"
        here = nested if attribute is not None else nested.get("block") or {}
    return ""
