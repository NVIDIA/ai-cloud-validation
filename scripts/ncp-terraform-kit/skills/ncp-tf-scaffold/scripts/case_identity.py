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
"""Which case is which, when one resource type has more than one.

A type's unnamed case is `<type>.yaml`. A further case carries
`resource.case_name` and is `<type>_<name>.yaml`. Every name a case renders --
its files, its workspace, its Go tests -- comes from its case id, so two cases
of one type never write one file. A lookup by type takes the unnamed case.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: A case name becomes part of a file name, a Go identifier and a record
#: entry, and these characters are the ones all three take.
CASE_NAME = re.compile(r"^[a-z0-9]+$")


def go_name(terraform_type: str) -> str:
    """A Terraform type as the Go identifier a test is named for.

    Underscore and hyphen are both word boundaries, and neither survives. An NCP
    named `my-isv` reaches Terraform as `my_isv_instance` and has to reach Go as
    `MyIsvInstance`: a hyphen is not a Go identifier character, and a test
    function carrying one does not compile.
    """
    return "".join(part.title() for part in re.split(r"[-_]+", terraform_type) if part)


def terraform_type(case: dict[str, Any]) -> str:
    return str((case.get("resource") or {}).get("terraform_address") or "").partition(".")[0]


def case_name(case: dict[str, Any]) -> str:
    return str((case.get("resource") or {}).get("case_name") or "")


def case_id(case: dict[str, Any]) -> str:
    """The type for an unnamed case, `<type>_<name>` for a named one."""
    name = case_name(case)
    return f"{terraform_type(case)}_{name}" if name else terraform_type(case)


def go_case_name(case: dict[str, Any]) -> str:
    """The type's Go name for an unnamed case, `<Type>_<name>` for a named one."""
    name = case_name(case)
    return f"{go_name(terraform_type(case))}_{name}" if name else go_name(terraform_type(case))


#: The form of a Terraform type and of a module name. Each names a rendered
#: file or directory, so a `/` or `..` in one writes outside `--out`.
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
#: A resource address: its type, then the resource's own name in Terraform.
ADDRESS = re.compile(r"^[a-z][a-z0-9_]*\.[A-Za-z_][A-Za-z0-9_-]*$")
_IDENTIFIER_FORM = "a lowercase letter followed by lowercase letters, digits and underscores"


def name_refusal(what: str, value: str) -> str | None:
    """Why `value` cannot name a rendered file, or None."""
    if IDENTIFIER.fullmatch(value):
        return None
    return f"{what} {value!r} is not {_IDENTIFIER_FORM}; it names the files that the kit writes"


def refusals(case: dict[str, Any]) -> list[str]:
    """What one case says about its own identity that cannot run."""
    broken = []
    address = str((case.get("resource") or {}).get("terraform_address") or "")
    if case.get("contract") == "resource" and not ADDRESS.fullmatch(address):
        broken.append(
            f"terraform_address {address!r} is not <type>.<name> with a type of "
            f"{_IDENTIFIER_FORM}; the type names the files that the kit writes"
        )
    if case.get("contract") == "module":
        broken += filter(None, [name_refusal("module", str(case.get("module") or ""))])
    name = (case.get("resource") or {}).get("case_name")
    if name is not None and not CASE_NAME.fullmatch(str(name)):
        broken.append(f"case_name {name!r} is not lowercase letters and digits; it names files and Go tests")
    return broken + proof_refusals(case)


#: The judged field that maps each requirement a case cites to the fixture
#: paths that exercise it.
PROVED_BY = "proved_by"
#: What an unset fixture value holds.
_UNSET: tuple[Any, ...] = (None, {}, [], "")


def _fixture_value(case: dict[str, Any], path: str) -> Any:
    here: Any = case
    for segment in path.split("."):
        if not isinstance(here, dict):
            return None
        here = here.get(segment)
    return here


def proof_refusals(case: dict[str, Any]) -> list[str]:
    """Each cited requirement that no value of the fixture is said to exercise.

    A requirement that the case sets no value for is answered by the API's
    default, and a pass then reads as proof of it. An empty `proved_by` is
    already refused as an unfilled judged field.
    """
    cited = (case.get("resource") or {}).get("requirements") or []
    proved = case.get(PROVED_BY)
    if case.get("contract") != "resource" or not cited or proved in _UNSET:
        return []
    if not isinstance(proved, dict):
        return [f"{PROVED_BY} is not a map from each cited requirement to fixture paths"]
    broken = []
    for requirement in cited:
        paths = proved.get(requirement)
        paths = [paths] if isinstance(paths, str) and paths else paths
        if not paths or not isinstance(paths, list):
            broken.append(
                f"{PROVED_BY} names no fixture value for {requirement}; a requirement "
                "that no value of the case sets is the API's default, and a pass would "
                "read as proof of it"
            )
            continue
        broken += [
            f"{PROVED_BY}.{requirement} names {path}, which the fixture does not set; it exercises nothing"
            for path in paths
            if not str(path).startswith("fixture.") or _fixture_value(case, str(path)) in _UNSET
        ]
    return broken


def scope_refusal(contract: str, resources: list[str] | None, name: str | None) -> str | None:
    """Why `instantiate --case-name` cannot run with this scope, or None."""
    if name is None:
        return None
    if not CASE_NAME.fullmatch(name):
        return f"--case-name {name!r} is not lowercase letters and digits; it names files and Go tests"
    if contract != "resource" or len(resources or ()) != 1:
        return (
            "--case-name writes a further case of one resource type: give "
            "--contract resource and exactly one --resource"
        )
    return None


def further_case(case: dict[str, Any], name: str) -> dict[str, Any]:
    """`case` as the further case `name` of its type.

    Its recordings are its own: a recording of the unnamed case holds that
    case's values, so each cassette name starts with the case id.
    """
    resource: dict[str, Any] = {}
    for key, value in (case.get("resource") or {}).items():
        resource[key] = value
        if key == "terraform_address":
            resource["case_name"] = name
    further = {**case, "resource": resource}
    if case.get("expected_requests"):
        further["expected_requests"] = [
            {**entry, "cassette": f"{case_id(further)}-{entry['verb']}"}
            if isinstance(entry, dict) and entry.get("cassette")
            else entry
            for entry in case["expected_requests"]
        ]
    return further


def collisions(cases: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    """Two cases with one case id: two unnamed cases of one type, two cases of
    one type with one name, or a case `<name>` of `<type>` beside the type
    `<type>_<name>`.

    Each pair would render the same files and the same workspace, and the
    record could not say which case an offline result is about.
    """
    seen: dict[str, Path] = {}
    broken = []
    for path, case in cases:
        if not isinstance(case, dict) or case.get("contract") != "resource":
            continue
        key = case_id(case)
        if key in seen:
            broken.append(
                f"{seen[key]} and {path} both render as {key}; give the further case "
                "a name of its own with `instantiate --case-name`"
            )
            continue
        seen[key] = path
    return broken
