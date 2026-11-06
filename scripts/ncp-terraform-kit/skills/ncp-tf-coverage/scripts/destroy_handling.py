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

"""`on_destroy`: what terraform destroy does with an object the API never deletes.

A resource with no delete, or whose delete the documents say is refused, cannot
remove its object. terraform destroy then either forgets the object, so it
stays in the account, or fails. Either one leaves something behind, so the
operator chooses it with the plan.
"""

from __future__ import annotations

from typing import Any

import operation_refs
from api_spec import Spec

KEY = "on_destroy"
#: Each handling, and what terraform destroy then does.
MEANING = {
    "forget": "terraform destroy removes the object from the state and warns. The object stays in the account.",
    "refuse": "terraform destroy fails, and the object stays in the account.",
}
_KEYS = frozenset({"handling", "reason", "quote"})


def _folded(text: Any) -> str:
    return " ".join(str(text or "").split())


def failures(where: str, resource: dict[str, Any], spec: Spec) -> list[str]:
    """A resource that plans no delete states `on_destroy`; one that plans a
    delete states it only with the sentence of the delete that refuses it."""
    operations = resource.get("operations") or {}
    entry = resource.get(KEY)
    if not operations:
        return []
    if entry is None:
        if operations.get("delete"):
            return []
        return [
            f"{where}: plans no delete, so terraform destroy cannot remove the object. State `{KEY}` with "
            f"a `handling`, forget or refuse, and the `reason`, for the operator to approve"
        ]
    if not isinstance(entry, dict):
        return [f"{where}: `{KEY}` holds a `handling` and a `reason`"]
    found = [f"{where}: `{KEY}` has no key `{key}`" for key in sorted(set(entry) - _KEYS)]
    if entry.get("handling") not in MEANING:
        found.append(f"{where}: `{KEY}.handling` is forget or refuse, for the operator to approve")
    if not _folded(entry.get("reason")):
        found.append(f"{where}: `{KEY}` gives the `reason` that the API never deletes the object")
    delete = operation_refs.resolve(str(operations.get("delete") or ""), spec)
    if operations.get("delete"):
        quote = _folded(entry.get("quote"))
        if delete is None or not quote or quote not in _folded(delete.description):
            found.append(
                f"{where}: plans a delete, so `{KEY}` quotes the sentence of the delete's description "
                f"that says the delete is refused"
            )
    return found


def refused(planned: list[dict[str, Any]]) -> set[str]:
    """The types whose destroy fails."""
    return {
        str(resource.get("terraform_type"))
        for resource in planned
        if isinstance(resource.get(KEY), dict) and resource[KEY].get("handling") == "refuse"
    }


def report_lines(planned: list[dict[str, Any]]) -> list[str]:
    """One decision group for each handling: the handling in bold, what it
    does, the other choice, and the types it applies to with the reason."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for resource in planned:
        entry = resource.get(KEY)
        if isinstance(entry, dict) and entry.get("handling") in MEANING:
            groups.setdefault(str(entry["handling"]), []).append(resource)
    lines: list[str] = []
    for handling, resources in groups.items():
        lines += [f"### **{handling}** for an object that the API never deletes (`{KEY}`)", "", MEANING[handling], ""]
        lines += [f"The other choice is **{other}**. {MEANING[other]}" for other in MEANING if other != handling]
        lines += ["", "| type | reason |", "|---|---|"]
        lines += [
            f"| {resource.get('terraform_type')} | {_cell(resource[KEY].get('reason'))} |" for resource in resources
        ]
        lines.append("")
    return lines[:-1]


def _cell(text: Any) -> str:
    return _folded(text).replace("|", "\\|")
