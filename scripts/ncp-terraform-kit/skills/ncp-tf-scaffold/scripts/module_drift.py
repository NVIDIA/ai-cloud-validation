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
"""Where a module case's values leave the reviewed resource case of each resource.

A reviewed resource case holds what that resource's own runs found: a field the
API needs though its document calls it optional, a path the API spells its own
way. A module case written apart from it repeats none of that, and only the
module's live run finds it again. So each value the resource case sets is
compared with what the module entry sends: the entry's own value, else the
shared input that fills it.

Not compared: an attribute that names the resource, or refers to another
resource, because the module sets those from its own keys; and a value that
holds a placeholder, because the run fills it.
"""

from __future__ import annotations

import copy
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

Path_ = tuple[str, ...]

#: A value the run fills in, so two cases may spell it differently.
PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}")
#: Where a module case records a difference it keeps on purpose.
ACCEPTED = "differs_from_resource_case"


def module_skill_pluralize() -> Callable[[str], str]:
    """The module skill's own variable naming, so both skills map one way."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        scripts = parent / "ncp-tf-module" / "scripts"
        if scripts.is_dir():
            sys.path.insert(0, str(scripts))
            import module_generator

            return module_generator.pluralize
    raise SystemExit("case_builder: no ncp-tf-module skill beside this one; install both skills")


def _flat(value: Any, prefix: Path_ = ()) -> dict[Path_, Any]:
    """Each leaf by its path; a list element by its index, so one field of one
    element is compared on its own."""
    if isinstance(value, dict) and value:
        items = [(str(key), inner) for key, inner in value.items()]
    elif isinstance(value, list) and value:
        items = [(f"[{index}]", inner) for index, inner in enumerate(value)]
    else:
        return {prefix: value}
    found: dict[Path_, Any] = {}
    for key, inner in items:
        found.update(_flat(inner, (*prefix, key)))
    return found


def _fill(value: Any, path: Path_, default: Any) -> None:
    """Fill `default` where the module would: a null or absent leaf takes it, in
    every element of a list on the way, and an optional object the entry does
    not set stays unset, because the module creates no object for it."""
    if isinstance(value, list):
        for element in value:
            _fill(element, path, default)
        return
    if not isinstance(value, dict):
        return
    head, rest = path[0], path[1:]
    if not rest:
        if value.get(head) is None:
            value[head] = copy.deepcopy(default)
        return
    if value.get(head) is not None:
        _fill(value[head], rest, default)


def _sent(
    entry: dict[str, Any], terraform_type: str, inputs: dict[str, Any], shared: dict[str, Any]
) -> dict[Path_, Any]:
    """What one module entry sends: its own values, then each shared input it leaves null."""
    sent = copy.deepcopy(entry or {})
    for name, targets in shared.items():
        dotted = (targets or {}).get(terraform_type)
        if dotted and name in inputs:
            _fill(sent, tuple(str(dotted).split(".")), inputs[name])
    return _flat(sent)


def resource_fixtures(directory: Path, load: Callable[[Path], Any]) -> dict[str, dict[str, Any]]:
    """Each reviewed resource case under `directory`, by Terraform type.

    A layer module uses one configuration of each resource, so a type's
    further, named cases are not what its entry is compared with.
    """
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.rglob("*.yaml")):
        case = load(path)
        if isinstance(case, dict) and case.get("contract") == "resource":
            resource = case.get("resource") or {}
            if resource.get("case_name"):
                continue
            kind = str(resource.get("terraform_address") or "").partition(".")[0]
            found[kind] = {
                "logical_kind": resource.get("logical_kind"),
                "create": (case.get("fixture") or {}).get("create") or {},
            }
    return found


def differences(
    case: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    review: dict[str, Any],
    pluralize: Callable[[str], str],
    note: Callable[[str], None] = lambda _: None,
) -> list[str]:
    """Each value the module case sends differently from the reviewed resource case.

    A resource with no reviewed case is noted, not refused: a resource whose
    live test the operator withheld has none, and its values have nothing to be
    compared with. A module none of whose resources has one is refused, because
    that is a wrong directory, and a comparison of nothing would pass.
    """
    inputs = case.get("variables") or {}
    skipped = {
        (str(r.get("resource")), tuple(str(r.get("attribute")).split("."))) for r in review.get("references") or []
    }
    skipped |= {(str(t), tuple(str(d).split("."))) for t, d in (review.get("name_attribute") or {}).items()}
    accepted = {
        (str(a.get("resource")), str(a.get("path"))): a for a in case.get(ACCEPTED) or [] if isinstance(a, dict)
    }
    used: set[tuple[str, str]] = set()
    found: list[str] = []
    types = [str(address).partition(".")[0] for address in case.get("resources") or []]
    if types and not any(t in resources for t in types):
        found.append(
            "no resource of this module has a reviewed resource case; "
            "is --resource-cases the provider's cases directory?"
        )
    for terraform_type in types:
        reviewed = resources.get(terraform_type)
        if not reviewed:
            note(f"{terraform_type} has no reviewed resource case; its values are not compared")
            continue
        variable = (review.get("variables") or {}).get(terraform_type) or pluralize(str(reviewed["logical_kind"]))
        want = _flat(reviewed["create"])
        for key, entry in sorted((inputs.get(variable) or {}).items()):
            sent = _sent(entry, terraform_type, inputs, review.get("shared") or {})
            for path, value in sorted(want.items()):
                bare = tuple(part for part in path if not part.startswith("["))
                if (terraform_type, bare) in skipped or PLACEHOLDER.search(str(value)):
                    continue
                if path in sent and sent[path] == value:
                    continue
                dotted = ".".join(path)
                if (terraform_type, dotted) in accepted:
                    used.add((terraform_type, dotted))
                    continue
                where = f"{variable}.{key}.{dotted}"
                found.append(
                    f"{where} is unset; the reviewed {terraform_type} case sets {value!r}"
                    if sent.get(path) is None
                    else f"{where} is {sent[path]!r}; the reviewed {terraform_type} case sets {value!r}"
                )
    for (terraform_type, dotted), entry in sorted(accepted.items()):
        if not str(entry.get("why") or "").strip():
            found.append(f"{ACCEPTED}: {terraform_type} {dotted} gives no why")
        elif (terraform_type, dotted) not in used:
            found.append(f"{ACCEPTED}: {terraform_type} {dotted} no longer differs; delete it")
    return found
