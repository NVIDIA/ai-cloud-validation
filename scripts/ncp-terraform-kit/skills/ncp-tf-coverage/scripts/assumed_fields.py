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
"""The fields whose documents are silent, and which of them the live update
changes.

A field that the update's body carries, with no sentence and no update call
that says it can change, updates in place by default. The plan lists each one
that it read and found no sentence for under `documents_silent`. A wrong
default fails only when something changes that field, so the operator sees
each one here, with the fields that no live update reaches. Both lists come
from the steps that act on them: the provider's derivation, and the
scaffold's own rule for the fields that a case's live update must change.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from replaced_types import Replaced

ASSUMED_HEADING = "Fields the documents are silent on"
LIST_HEADING = "Each field the documents are silent on"


def _scaffold_scripts() -> Path:
    """The sibling skill's scripts. The skills install together, so the
    sibling is always beside this one."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        sibling = parent / "ncp-tf-scaffold" / "scripts"
        if sibling.is_dir():
            return sibling
    raise SystemExit(
        f"api_coverage_report: no ncp-tf-scaffold skill beside {here.parents[1]}. "
        f"The report shows which assumed fields the live update changes as the scaffold derives them; "
        f"install both skills"
    )


def live_changes(coverage: dict[str, Any], spec_files: tuple[Path, ...]) -> dict[str, set[str]]:
    """The document paths that each type's live update changes, as the
    scaffold derives `update_fields.change`."""
    sys.path.insert(0, str(_scaffold_scripts()))
    import case_builder
    import in_place
    import update_fields

    operations = case_builder.index_operations([case_builder.load_document(path) for path in spec_files])
    updated = in_place.derive(coverage, spec_files)
    found: dict[str, set[str]] = {}
    for resource in case_builder.resources_of(coverage):
        pairs = updated.get(resource.terraform_type, ())
        changed = set(update_fields.derive(resource, operations, pairs).get("change") or [])
        found[resource.terraform_type] = {path for path, terraform in pairs if terraform in changed}
    return found


def unreached(replaced: Replaced, changed: set[str]) -> tuple[str, ...]:
    """The assumed fields that the live update does not change: an object is
    reached when the live update changes a field inside it."""
    return tuple(
        path for path in replaced.assumed if not any(done == path or done.startswith(f"{path}.") for done in changed)
    )


def _silent(found: list[Replaced], silent: dict[str, set[str]] | None) -> list[Replaced]:
    """Each type with only the assumed fields that the plan lists as silent:
    an override or a condition decides the others."""
    if silent is None:
        return found
    return [
        replace(r, assumed=tuple(path for path in r.assumed if path in silent.get(r.terraform_type, set())))
        for r in found
    ]


def review_lines(
    found: list[Replaced], reached: dict[str, set[str]], silent: dict[str, set[str]] | None = None
) -> list[str]:
    """The count of silent fields for each type, under the changes that
    replace: the operator reviews where a wrong fact can hide, not each field."""
    rows = [
        f"| {r.terraform_type} | {len(r.assumed)} | {len(unreached(r, reached.get(r.terraform_type, set())))} |"
        for r in _silent(found, silent)
        if r.assumed
    ]
    if not rows:
        return []
    return [
        "",
        f"### {ASSUMED_HEADING}",
        "",
        "The documents say nothing about whether these fields can change. The plan read each one and "
        "lists it under `documents_silent`, and it updates the field in place because the update's body "
        "carries it. A wrong fact can hide here. The live update test changes each field that it "
        "reaches, and an API refusal there shows a wrong one. Correct it with a `field_overrides` entry "
        "in `coverage.yaml`. A field that the live update does not reach is checked by nothing before a "
        "real change: look at those first. Each field is listed under Reference.",
        "",
        "| type | silent fields | not changed by the live update |",
        "|---|---|---|",
        *rows,
    ]


def reference_lines(
    found: list[Replaced], reached: dict[str, set[str]], silent: dict[str, set[str]] | None = None
) -> list[str]:
    """Each silent field, by type, with the ones the live update does not change."""
    lines = []
    for replaced in (r for r in _silent(found, silent) if r.assumed):
        missed = unreached(replaced, reached.get(replaced.terraform_type, set()))
        tested = [path for path in replaced.assumed if path not in missed]
        parts = [f"changed by the live update: {', '.join(tested)}"] if tested else []
        parts += [f"not exercised by the live update: {', '.join(missed)}"] if missed else []
        lines.append(f"- {replaced.terraform_type}: {'; '.join(parts)}")
    if not lines:
        return []
    return [
        "",
        f"## {LIST_HEADING}",
        "",
        "Each field that the plan updates in place because the update's body carries it, and whose "
        "documents state no sentence about it.",
        "",
        *lines,
    ]
