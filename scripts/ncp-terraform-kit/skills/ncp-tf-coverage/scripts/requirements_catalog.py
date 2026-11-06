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

"""The requirement catalog, in whichever form the validation repository publishes.

Two forms reach us, and they say the same things. The earlier one is a YAML list
with a field per attribute. The current one is the published Markdown guide: a
requirement is a table row whose first cell is a bold identifier, and the
headings above the table say where it belongs. Both reduce to one map here, so
nothing downstream branches on which arrived.

**Where a requirement belongs is read from the headings, not from a field.** The
Markdown guide states it structurally -- chapter, section, and the deepest
heading that owns the table -- and that structure is what a reader sees. Deriving
it from the headings keeps the catalog and the mapping in agreement by
construction: there is no second place to edit and forget.

**A chapter a Terraform integration cannot serve is carried, not dropped.** The
guide's operational chapter asks for ticketing, escalation and on-call practice.
None of it is a resource any provider creates, so no analysis of an API can
answer it. Dropping those requirements at the door would leave an operator
reading a report that never mentions them, which reads exactly like a catalog
that does not contain them. They are loaded, marked, reported, and exempt from
the rule that every requirement owes an analysis entry.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

#: A chapter -- the guide's top heading level -- whose requirements no Terraform
#: integration can satisfy. Named here rather than inferred, because "this asks
#: for a practice rather than an API" is a judgement, and a judgement belongs
#: where a reader can find it and disagree with it.
UNSERVED_CHAPTERS = ("Operational Requirements",)

#: `| **SEC01** | ...` -- the identifier is bold, and bold is what separates a
#: requirement row from the version history table that opens the document.
_REQUIREMENT_ID = re.compile(r"^\*\*([A-Z][A-Z0-9]*)\*\*$")

#: The first header cell of a requirement table. Each row under it is a
#: requirement, so it must read as one.
_REQUIREMENT_HEADER = "Req ID"

#: A table's header separator cell, `:----` or `---`.
_SEPARATOR = re.compile(r"^:?-+:?$")

#: A heading, at any level this document uses for structure.
_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*#*$")

#: A cell boundary. A pipe inside a cell is escaped, and splitting on an escaped
#: one would cut a description in half and shift every later cell left.
_CELL = re.compile(r"(?<!\\)\|")


class CatalogError(Exception):
    """The catalog is missing, unreadable, or says two things about one id."""


def load(path: Path) -> dict[str, dict[str, Any]]:
    """The catalog at `path`, keyed by `req_id`, whichever form wrote it.

    The suffix decides. A catalog in any other form is refused with the suffix
    named, because guessing at the content of a file the operator pointed us at
    is how a run reads half a document and reports the half.
    """
    if path.suffix.lower() in {".md", ".markdown"}:
        return _from_markdown(_read(path), path)
    if path.suffix.lower() in {".yaml", ".yml"}:
        return _from_yaml(path)
    raise CatalogError(
        f"{path} is neither Markdown nor YAML. This reads the published guide (.md) and the YAML catalog (.yaml)"
    )


def served(requirements: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Only the requirements a Terraform integration can be judged against."""
    return {req_id: entry for req_id, entry in requirements.items() if entry.get("served", True)}


def unserved(requirements: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Only the requirements no Terraform integration can answer."""
    return {req_id: entry for req_id, entry in requirements.items() if not entry.get("served", True)}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise CatalogError(f"no such file: {path}") from error


def _from_yaml(path: Path) -> dict[str, dict[str, Any]]:
    """The earlier form: a `requirements` list, one mapping per requirement."""
    try:
        document = yaml.safe_load(_read(path))
    except yaml.YAMLError as error:
        raise CatalogError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(document, dict) or "requirements" not in document:
        raise CatalogError(f"{path} has no 'requirements' list")
    entries: dict[str, dict[str, Any]] = {}
    for entry in document["requirements"]:
        entries[entry["req_id"]] = {**entry, "served": True}
    return entries


def _from_markdown(text: str, path: Path) -> dict[str, dict[str, Any]]:
    """The published guide: bold identifiers in tables, under nested headings."""
    entries: dict[str, dict[str, Any]] = {}
    chapter = section = subsection = ""
    in_requirement_table = False

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("|"):
            in_requirement_table = False
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
            if level == 2:
                chapter, section, subsection = title, "", ""
            elif level == 3:
                section, subsection = title, ""
            else:
                # Level 4 and deeper both name the table that follows, and the
                # deepest one wins: a chapter that nests one level further is
                # still saying "these rows are about this".
                subsection = title
            continue

        cells = _cells(line)
        if cells and cells[0] == _REQUIREMENT_HEADER:
            in_requirement_table = True
            continue
        if not cells or _SEPARATOR.match(cells[0]):
            continue
        looks_like_one = in_requirement_table or cells[0].startswith("**")
        identity = _REQUIREMENT_ID.match(cells[0])
        if not looks_like_one:
            continue
        if not identity or len(cells) < 3:
            # A skipped row would vanish from every report and gate, and a new
            # catalog revision changes the count that could have shown it.
            raise CatalogError(
                f"{path}:{number}: this row looks like a requirement and does not read as one: "
                f"{line.strip()[:80]!r}. A row is `| **ID** | area | description |` on one line, "
                f"with the identifier in bold"
            )
        req_id = identity.group(1)
        if req_id in entries:
            raise CatalogError(
                f"{path}:{number}: {req_id} appears twice. A catalog that says "
                f"two things about one requirement cannot be mapped"
            )
        entries[req_id] = {
            "req_id": req_id,
            "chapter": chapter,
            "section": section or chapter,
            "subsection": subsection,
            "area": cells[1],
            # Every cell after the area, not only the first. One row carries a
            # fourth column holding "within 5% of a target", and a gate reads
            # the description for exactly that kind of stated quantity.
            "description": " ".join(cell for cell in cells[2:] if cell),
            "served": chapter not in UNSERVED_CHAPTERS,
        }

    if not entries:
        raise CatalogError(
            f"{path} holds no requirement row. A row is `| **ID** | area | description |`, with the identifier in bold"
        )
    return entries


def _cells(line: str) -> list[str]:
    """One table row's cells, or nothing when the line is not a row."""
    trimmed = line.strip()
    if not trimmed.startswith("|"):
        return []
    body = trimmed[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL.split(body)]


def slug(heading: Any) -> str:
    """A heading, spelled the way a directory can carry it."""
    return re.sub(r"[^a-z0-9]+", "-", str(heading).lower()).strip("-")


def headings_of(req_ids: Sequence[str], catalog: dict[str, dict[str, Any]]) -> str:
    """Every spelling of the headings a requirement sits under, for matching.

    The section, the subsection, and each one's directory form, lowercased and
    run together: an operator names a heading the way they say it, and the
    catalog spells it the way the document does.
    """
    parts: list[str] = []
    for req_id in req_ids:
        entry = catalog.get(str(req_id)) or {}
        for raw in (entry.get("section", ""), entry.get("subsection", "")):
            parts += [str(raw).lower(), slug(raw)]
    return " ".join(parts)


def narrowed_to_sections[T](
    items: Sequence[T],
    wanted: Sequence[str],
    catalog: dict[str, dict[str, Any]],
    requirements_of: Callable[[T], Sequence[str]],
) -> list[T]:
    """Only the items filed under a heading the operator named.

    Matched as a case-insensitive substring, because an operator says `IAM`
    rather than `Identity and Access Management (IAM)`.

    A name that matches nothing is refused rather than ignored. Narrowing to a
    heading that is not there would otherwise run over everything, or over
    nothing, and report a success either way.

    Both the scaffold and the provider narrow this way, over different shapes --
    a dataclass and a mapping -- so what each item's requirements are is the
    caller's to say and everything else is here. Two copies of a matching rule
    is two answers to "did this run cover my section".
    """
    needles = tuple(name.strip().lower() for name in wanted if name.strip())
    if not needles:
        return list(items)

    kept: list[T] = []
    matched: set[str] = set()
    for item in items:
        headings = headings_of(requirements_of(item), catalog)
        for needle in needles:
            if needle in headings:
                kept.append(item)
                matched.add(needle)
                break
    missing = [n for n in needles if n not in matched]
    if missing:
        # The names only: what a caller calls the things it was narrowing is
        # the caller's word, and it is the half that makes the message useful.
        raise CatalogError(", ".join(missing))
    return kept


def plan_requirement_ids(entry: dict[str, Any]) -> list[str]:
    """The requirements one planned resource names, however the plan spells them.

    A plan writes a requirement as a bare id, or as a mapping carrying a
    `part_id` when the requirement was cut into parts.
    """
    found: list[str] = []
    for item in entry.get("requirements") or []:
        name = str(item.get("req_id") or "") if isinstance(item, dict) else str(item)
        if name and name not in found:
            found.append(name)
    return found


def filed_requirements(resources: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """The requirements each planned resource is filed under, by `terraform_type`.

    A resource a requirement names is filed under its own. A dependency resource
    names none: it is filed where the first resource it serves is filed, so its
    case sits beside the case that needs it and narrowing to a heading keeps the
    two together. A chain that reaches no requirement files nowhere, and the
    coverage gate refuses that plan.
    """
    planned = {
        str(entry["terraform_type"]): entry
        for entry in resources
        if isinstance(entry, dict) and entry.get("terraform_type")
    }

    def resolve(kind: str, seen: frozenset[str]) -> list[str]:
        entry = planned.get(kind)
        if entry is None or kind in seen:
            return []
        own = plan_requirement_ids(entry)
        if own:
            return own
        served = entry.get("dependency_of")
        for name in served if isinstance(served, list) else []:
            found = resolve(str(name), seen | {kind})
            if found:
                return found
        return []

    return {kind: resolve(kind, frozenset()) for kind in planned}
