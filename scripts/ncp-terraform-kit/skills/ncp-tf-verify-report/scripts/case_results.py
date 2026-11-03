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
"""Which record entry each case of a resource type has, read from the cases.

A type's unnamed case is the entry `<type>`. A further case that carries
`resource.case_name` is the entry `<type>[<name>]`, with its own lifecycle test,
`TestAcc<Type>_<name>_lifecycle`, and its own offline subject, `<type>_<name>`.
A requirement that several cases of one type cite is proved only when each of
them passed, so each one is an entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import intermittent
import not_selected
import observed
import tier
import yaml

#: A test result as the evidence states it.
PAST = {"pass": "passed", "fail": "failed", "error": "errored", "skip": "skipped"}

#: How a further case's entry is keyed: its type, and its name in brackets.
NAMED_ENTRY = re.compile(r"^(?P<type>[^\[\]]+)\[(?P<name>[a-z0-9]+)\]$")


@dataclass(frozen=True)
class Subject:
    """One case's entry in the record, and the names its results arrive under."""

    entry: str
    terraform_type: str
    test: str
    offline: str


def go_name(terraform_type: str) -> str:
    """A Terraform type as the Go identifier its test is named for.

    The same rule the scaffold uses to write the name. Inverting a Go identifier
    is guesswork -- `GcpGroupMembership` splits three ways -- so the plan's own
    types are converted forward and matched, and a name matching none is
    reported rather than assigned to whichever type looks closest.
    """
    return "".join(part.title() for part in re.split(r"[-_]+", terraform_type) if part)


def type_of(entry: str) -> str:
    """The resource type a record entry is about."""
    matched = NAMED_ENTRY.match(entry)
    return matched.group("type") if matched else entry


def is_named(entry: str) -> bool:
    return NAMED_ENTRY.match(entry) is not None


def subjects(directory: Path, planned: set[str]) -> tuple[dict[str, Subject], list[str]]:
    """An entry for each planned type and each named case, and the strays.

    A stray is a named case whose type the plan does not carry: it has no
    requirement to count towards and no layer to sit in.
    """
    if not directory.is_dir():
        raise ValueError(f"no cases directory at {directory}")
    cases = _resource_cases(directory)
    if not cases:
        raise ValueError(
            f"{directory} holds no resource case; give the provider's cases directory, "
            "so each named case of a type is recorded beside it"
        )
    found = {kind: Subject(kind, kind, f"TestAcc{go_name(kind)}_lifecycle", kind) for kind in planned}
    strays = []
    for kind, name in sorted({(_type(c), _name(c)) for c in cases}):
        if not name:
            continue
        entry = f"{kind}[{name}]"
        if kind not in planned:
            strays.append(entry)
            continue
        found[entry] = Subject(entry, kind, f"TestAcc{go_name(kind)}_{name}_lifecycle", f"{kind}_{name}")
    _refuse_a_shared_offline_subject(found)
    return found, strays


def _refuse_a_shared_offline_subject(found: dict[str, Subject]) -> None:
    """A case `<name>` of `<type>` runs as `<type>_<name>`, which can be another
    planned type. One offline result cannot say which of the two it is about."""
    seen: dict[str, str] = {}
    for entry, subject in sorted(found.items()):
        if subject.offline in seen:
            raise ValueError(
                f"{seen[subject.offline]} and {entry} both run as the offline subject "
                f"{subject.offline}; give the named case another name"
            )
        seen[subject.offline] = entry


def named_tests(found: dict[str, Subject]) -> dict[str, str]:
    """Each named case's entry, by the Go name its lifecycle test carries."""
    return {
        subject.test.removeprefix("TestAcc").removesuffix("_lifecycle"): entry
        for entry, subject in found.items()
        if is_named(entry)
    }


def resource_entry(
    entry: dict[str, Any],
    subject: Subject,
    live: str | None,
    offline: str | None,
    keep_cited: set[str],
    skipped: dict[str, Any] | None = None,
    printed: str | None = None,
) -> dict[str, Any]:
    """One case's entry after a run, from its live and its offline result.

    `skipped` is the selection's entry when the operator chose not to build the type.
    `printed` is what the lifecycle test printed that the record may keep.
    """
    if live == "pass":
        entry["status"] = "live"
        entry["evidence"] = f"{subject.test} passed"
        intermittent.after_a_pass(entry, keep_cited)
    elif live in {"fail", "skip"}:
        entry["status"] = "offline"
        entry["evidence"] = observed.joined(f"{subject.test} {PAST[live]}", printed)
        entry.setdefault("blocked_by", [])
    elif offline == "pass" and tier.keeps(entry):
        # An offline pass shows less than a live run already recorded, whether
        # it passed or failed, so it is not news about this resource. The entry
        # stays, and so does its tier; an offline failure is news.
        return entry
    elif offline == "pass":
        # Recorded, not inferred: a resource whose offline test passed and that
        # was never run live is not the same as one with no tests.
        entry["status"] = "offline"
        entry["evidence"] = f"{subject.entry}'s offline test passed; no live run has reported it"
        entry.setdefault("blocked_by", [])
    elif offline in {"fail", "error", "skip"}:
        entry["status"] = "unverified"
        entry["evidence"] = f"{subject.entry}'s offline test {PAST[offline]}, so nothing has been shown"
    elif skipped is not None:
        return not_selected.recorded(entry, skipped)
    else:
        not_selected.cleared(entry)
        entry.setdefault("status", "unverified")
        entry.setdefault("evidence", "no run has reported this resource")
    if live or offline:
        entry[tier.FIELD] = "live" if live else "offline"
    return entry


def _resource_cases(directory: Path) -> list[dict[str, Any]]:
    found = []
    for path in sorted(directory.rglob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise ValueError(f"{path} is not readable as YAML: {error}") from error
        if isinstance(document, dict) and document.get("kind") == "case" and document.get("contract") == "resource":
            found.append(document)
    return found


def _type(case: dict[str, Any]) -> str:
    return str((case.get("resource") or {}).get("terraform_address") or "").partition(".")[0]


def _name(case: dict[str, Any]) -> str:
    return str((case.get("resource") or {}).get("case_name") or "")
