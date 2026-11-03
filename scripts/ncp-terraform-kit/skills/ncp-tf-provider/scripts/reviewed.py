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
"""What a reviewer decided in a derived schema, kept across a new derivation.

`schema` derives from the documents alone, so a field only a person sets is
absent from every derivation. `sensitive` is one: a secret is a reading of a
description, not a fact a document marks, and an unmarked secret is printed
in full by Terraform's diagnostics. So the derivation reads the file it is
about to replace, and carries each reviewed field to the attribute with the
same path. A field whose attribute is gone is reported, not dropped in silence.

A `naming` answer says which field names the object. The derivation reads
it before it marks the fields, and this module reports what it kept.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

#: Attribute fields a person sets and a derivation never emits.
REVIEWED = ("sensitive",)

Path = tuple[str, ...]


def _walk(attributes: list[Any], prefix: Path = ()) -> dict[Path, dict[str, Any]]:
    found: dict[Path, dict[str, Any]] = {}
    for attribute in attributes or []:
        if not isinstance(attribute, dict):
            continue
        path = (*prefix, str(attribute.get("api_name") or attribute.get("name")))
        found[path] = attribute
        found.update(_walk(attribute.get("attributes") or [], path))
    return found


def _by_type(document: dict[str, Any]) -> dict[str, dict[Path, dict[str, Any]]]:
    return {
        str(resource.get("terraform_type")): _walk(resource.get("attributes") or [])
        for resource in document.get("resources") or []
        if isinstance(resource, dict)
    }


#: json.dumps's indent when a file states none of its own.
DEFAULT_INDENT = 2


def indent_of(text: str) -> int:
    """The indent a JSON file was written with.

    A new derivation written with another indent rewrites every line, so a
    review diff of a real change is buried, and every line is scanned again as
    new.
    """
    lines = text.splitlines()
    if len(lines) < 2:
        return DEFAULT_INDENT
    second = lines[1]
    return (len(second) - len(second.lstrip(" "))) or DEFAULT_INDENT


def is_derivation(document: Any) -> bool:
    """Whether a file is a `schema` output this run may read and replace."""
    return isinstance(document, dict) and isinstance(document.get("resources"), list)


def previous(out: str | None) -> tuple[dict[str, Any] | None, int]:
    """The derivation at `out` that a new one replaces, if there is one, and its indent.

    Raises ValueError for a file that is not a derivation: writing over it
    would destroy a file that this run did not write.
    """
    if not out or not pathlib.Path(out).is_file():
        return None, DEFAULT_INDENT
    text = pathlib.Path(out).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"--out {out} is not JSON ({error}); name another file") from error
    if not is_derivation(document):
        raise ValueError(f"--out {out} is not a schema derivation; name another file")
    return document, indent_of(text)


def carry(previous: dict[str, Any], derived: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Copy each reviewed field from `previous` into `derived`, in place.

    Returns what was kept, and what had no attribute left to go to.
    """
    old = _by_type(previous)
    new = _by_type(derived)
    kept: list[str] = []
    lost: list[str] = []
    for terraform_type, attributes in sorted(old.items()):
        for path, attribute in sorted(attributes.items()):
            for key in REVIEWED:
                if key not in attribute:
                    continue
                where = f"{terraform_type}.{'.'.join(path)} {key}"
                target = (new.get(terraform_type) or {}).get(path)
                if target is None:
                    # The new file replaces the old one, so a flag on a
                    # resource this run left out is gone as well.
                    lost.append(where)
                    continue
                target[key] = attribute[key]
                kept.append(where)
    _carry_identifiers(previous, derived, kept, lost)
    _report_naming(previous, derived, kept, lost)
    return kept, lost


#: The `identifier.source` a person writes, when the documents do not say.
REVIEW = "review"


def _carry_identifiers(previous: dict[str, Any], derived: dict[str, Any], kept: list[str], lost: list[str]) -> None:
    """Copy a reviewed identifier to a resource whose documents still do not say.

    A source the derivation finds replaces the answer: the documents now state
    it, and a reading of them is no longer needed.
    """
    new = {str(r.get("terraform_type")): r for r in derived.get("resources") or [] if isinstance(r, dict)}
    for resource in previous.get("resources") or []:
        answer = resource.get("identifier") if isinstance(resource, dict) else None
        if not isinstance(answer, dict) or answer.get("source") != REVIEW:
            continue
        where = f"{resource.get('terraform_type')} identifier"
        target = new.get(str(resource.get("terraform_type")))
        if target is None:
            lost.append(where)
        elif not (target.get("identifier") or {}).get("source"):
            target["identifier"] = {**(target.get("identifier") or {}), **answer}
            kept.append(where)


def naming_answers(previous: dict[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    """Each `naming` answer of a reviewed derivation, by type and parameter.

    The derivation applies them, because whether a field forces a replacement
    depends on the planned calls, which only the derivation reads.
    """
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for resource in (previous or {}).get("resources") or []:
        if not isinstance(resource, dict):
            continue
        for entry in resource.get("naming") or []:
            if isinstance(entry, dict) and entry.get("source") == REVIEW and entry.get("parameter"):
                found.setdefault(str(resource.get("terraform_type")), {})[str(entry["parameter"])] = entry
    return found


def _report_naming(previous: dict[str, Any], derived: dict[str, Any], kept: list[str], lost: list[str]) -> None:
    """Say which `naming` answers the new derivation kept, and which it has no entry for."""
    new = {
        (str(r.get("terraform_type")), str(e.get("parameter")))
        for r in derived.get("resources") or []
        if isinstance(r, dict)
        for e in r.get("naming") or []
        if isinstance(e, dict) and e.get("source") == REVIEW
    }
    for terraform_type, answers in sorted(naming_answers(previous).items()):
        for parameter in sorted(answers):
            where = f"{terraform_type} naming {parameter}"
            (kept if (terraform_type, parameter) in new else lost).append(where)
