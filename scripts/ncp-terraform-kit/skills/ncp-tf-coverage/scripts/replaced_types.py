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

"""Which changes replace each planned type, as the provider derives them.

The provider step marks an attribute `forces_replacement` when no planned call
can change it, and the operator approves the plan from this report. The report
reads the provider's own derivation, so the two cannot disagree: a rule kept
here would drift from the one the provider applies.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SETTABLE = ("required", "optional")


@dataclass(frozen=True)
class Replaced:
    """One planned type that a change replaces, or may replace."""

    terraform_type: str
    #: Each field whose change replaces the type, in attribute order.
    fields: tuple[str, ...]
    #: True when every settable field replaces it.
    every: bool
    #: Each one-way field, with the direction its call cannot make.
    one_way: tuple[tuple[str, str], ...]
    #: Each field that only a request field at another path could change. The
    #: provider step records which one does; the rest replace.
    left_to_agent: tuple[str, ...]
    #: Where each replacing field comes from, by path, when not the request
    #: shapes: a quoted sentence, or the operator's own entry.
    sources: dict[str, str] = field(default_factory=dict)
    #: Each settable field that the update changes in place only because its
    #: body carries the field: no sentence and no update call says so.
    assumed: tuple[str, ...] = ()

    @property
    def replaces(self) -> bool:
        return bool(self.fields or self.one_way or self.left_to_agent)


#: The labels of a replacing field's source. `derived`: no planned call can
#: change the field, from the request shapes. `document`: a sentence the plan
#: quotes. `operator`: the operator's `field_overrides` entry.
DERIVED, DOCUMENT, OPERATOR = "derived", "document", "operator"


def _provider_scripts() -> Path:
    """The sibling skill's scripts. The skills install together, so the
    sibling is always beside this one."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        sibling = parent / "ncp-tf-provider" / "scripts"
        if sibling.is_dir():
            return sibling
    raise SystemExit(
        f"api_coverage_report: no ncp-tf-provider skill beside {here.parents[1]}. "
        f"The report shows the changes that replace as the provider derives them; install both skills"
    )


def provider_resources(coverage: dict[str, Any], spec_files: tuple[Path, ...]) -> list[Any]:
    """The provider's derivation of each planned type, with its attributes."""
    sys.path.insert(0, str(_provider_scripts()))
    import provider_schema

    methods, schemas = provider_schema.load_documents(list(spec_files))
    return provider_schema.derive(coverage, methods, schemas)


def derive(
    coverage: dict[str, Any], spec_files: tuple[Path, ...], resources: list[Any] | None = None
) -> list[Replaced]:
    """Each planned type that a change replaces, or whose fields the plan
    assumes change in place, from the provider's derivation."""
    if resources is None:
        resources = provider_resources(coverage, spec_files)
    from replacement import ASSUMED

    overridden = _overridden(coverage)
    found: list[Replaced] = []
    for resource in resources:
        settable = [a.name for a in resource.attributes if set(SETTABLE) & set(a.terraform_flags)]
        marked = tuple(a.name for a in resource.attributes if a.forces_replacement)
        every = bool(marked) and set(marked) == set(settable)
        fields = marked + tuple(
            path for a in resource.attributes if not a.forces_replacement for path in marked_within(a)
        )
        one_way = tuple((a.name, a.replaces_when) for a in resource.attributes if a.replaces_when)
        left = tuple(resource.replacement_left_to_agent)
        sources = {path: source for path, source in resource.replacement_sources.items() if path in fields}
        sources |= {name: DOCUMENT for name, _ in one_way}
        sources |= dict.fromkeys(overridden.get(resource.terraform_type, set()) & set(fields), OPERATOR)
        assumed = tuple(path for path, source in resource.replacement_sources.items() if source == ASSUMED)
        if fields or one_way or left or assumed:
            found.append(Replaced(resource.terraform_type, fields, every, one_way, left, sources, assumed))
    return found


def _overridden(coverage: dict[str, Any]) -> dict[str, set[str]]:
    """Each field that the operator's `field_overrides` entry says replaces, by type."""
    layers = ((coverage or {}).get("module_plan") or {}).get("layers") or {}
    return {
        str(entry.get("terraform_type") or ""): {
            str(o.get("field"))
            for o in entry.get("field_overrides") or []
            if isinstance(o, dict) and o.get("decision") == "replaces"
        }
        for body in layers.values()
        for entry in body.get("resources") or []
    }


def marked_within(attribute: Any) -> list[str]:
    """The path of each nested field that the provider marks: a call that
    changes part of a field leaves the rest of it to replace."""
    found: list[str] = []
    for child in getattr(attribute, "attributes", ()):
        if child.forces_replacement:
            found.append(f"{attribute.name}.{child.name}")
        else:
            found += [f"{attribute.name}.{path}" for path in marked_within(child)]
    return found


def _change(replaced: Replaced) -> str:
    """The fields that replace the type, grouped by where each comes from."""
    if replaced.every and not replaced.sources:
        return f"every change ({DERIVED})"
    named = [
        *((name, name) for name in replaced.fields),
        *((name, f"{name} on {_article(way)} {way}") for name, way in replaced.one_way),
    ]
    groups = [
        (source, [text for name, text in named if replaced.sources.get(name, DERIVED) == source])
        for source in (OPERATOR, DOCUMENT, DERIVED)
    ]
    return "; ".join(f"{source}: {', '.join(texts)}" for source, texts in groups if texts)


def _article(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"


#: The fields a row lists before it counts the rest: the provider schema's
#: notes name every one, and a row of fifty names is not read.
SHOWN = 8


def _left(replaced: Replaced) -> str:
    names = list(replaced.left_to_agent)
    if len(names) <= SHOWN:
        return ", ".join(names)
    return f"{', '.join(names[:SHOWN])} and {len(names) - SHOWN} more"


def report_lines(found: list[Replaced]) -> list[str]:
    """The report's table of the changes that replace a resource."""
    found = [r for r in found if r.replaces]
    if not found:
        return []
    rows = [f"| {r.terraform_type} | {_change(r)} | {_left(r)} |" for r in found]
    return [
        "",
        "## Changes that replace",
        "",
        "Terraform destroys these resources and creates them again on such a change, which can "
        "lose data. The provider marks the same fields. Each field says where it comes from: "
        f"`{DOCUMENT}` is a sentence that the plan quotes, `{DERIVED}` means that no planned call can "
        f"change it, and `{OPERATOR}` is your own `field_overrides` entry. A field left to your reading "
        "is one that only a request field at another path could change: the provider step records "
        "which one, and the others replace. Approve it with the plan.",
        "",
        "| type | a change that replaces it | left to your reading |",
        "|---|---|---|",
        *rows,
    ]
