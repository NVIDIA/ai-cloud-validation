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

"""The objects that the documents create and that the plan never mentions.

The completeness gates ask only about what the analysis names: a cited
creation, its enclosing collections, a planned type's nested creations. A
requirement marked `absent` cites nothing, so the object that would serve it is
never asked about, and the report cannot show that the agent missed it. This
lists each creation in the supplied documents that the plan neither plans,
cites, offers as a scope alternative, manages as an entry, nor excuses. It is
shown for the operator to scan, not gated: most such creations serve no
requirement, and which do is a reading of the requirement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import operation_refs
from api_spec import Spec

TITLE = "Objects the documents create that the plan does not mention"


def _evidence(mapping: dict[str, Any]) -> list[str]:
    """A requirement's evidence, and that of each of its parts."""
    parts = mapping.get("parts") or []
    return [
        *(mapping.get("evidence") or []),
        *(ref for part in (parts if isinstance(parts, list) else []) for ref in part.get("evidence") or []),
    ]


def _mentioned(coverage: dict[str, Any], module_plan: dict[str, Any]) -> set[str]:
    """Every operation reference that the analysis or the plan names."""
    named = {str(ref) for mapping in coverage.get("mappings") or [] for ref in _evidence(mapping)}
    for layer in (module_plan.get("layers") or {}).values():
        for resource in (layer or {}).get("resources") or []:
            named.update(str(ref) for ref in (resource.get("operations") or {}).values())
            named.update(str(ref) for ref in resource.get("scope_alternatives") or [])
            named.update(str(ref) for ref in resource.get("entry_methods") or [])
    named.update(
        str(entry["operation"])
        for entry in module_plan.get("unplanned_operations") or []
        if isinstance(entry, dict) and entry.get("operation")
    )
    return named


def unmentioned(
    coverage: dict[str, Any], module_plan: dict[str, Any], documents: dict[Path, Spec], spec: Spec
) -> dict[str, list[str]]:
    """Each document's creations that the plan never names, by file name.

    A creation is what `plan-completeness` calls one, read in the document
    that defines it, so the two cannot disagree on what creates.
    """
    named = {operation_refs.identity(ref, spec) for ref in _mentioned(coverage, module_plan)}
    found: dict[str, list[str]] = {}
    for path, document in sorted(documents.items(), key=lambda item: item[0].name):
        missing = sorted(
            operation_refs.cited(operation)
            for operation in document.every_operation()
            if operation_refs.creates(operation, document) and operation not in named
        )
        if missing:
            found[path.name] = missing
    return found


def _short(reference: str) -> str:
    return reference.removeprefix(operation_refs.OPERATION_PREFIX)


def report_lines(
    coverage: dict[str, Any], module_plan: dict[str, Any], documents: dict[Path, Spec], spec: Spec
) -> list[str]:
    """One line for each document: the count and each creation it holds that
    the plan never names."""
    found = unmentioned(coverage, module_plan, documents, spec)
    if not found:
        return []
    total = sum(len(refs) for refs in found.values())
    lines = [
        f"### {TITLE}",
        "",
        f"The documents create {total} kind(s) of object that this plan does not plan, cite or "
        "excuse, so no provider resource, module or test builds them and no requirement claims "
        "them. Most serve no requirement. Scan the list for one that a requirement needs, above "
        "all one marked absent: ask for its type, or approve the omission.",
        "",
    ]
    lines += [
        f"- **{name}** ({len(refs)}): {', '.join(f'`{_short(ref)}`' for ref in refs)}" for name, refs in found.items()
    ]
    return lines


def count(coverage: dict[str, Any], module_plan: dict[str, Any], documents: dict[Path, Spec], spec: Spec) -> int:
    return sum(len(refs) for refs in unmentioned(coverage, module_plan, documents, spec).values())
