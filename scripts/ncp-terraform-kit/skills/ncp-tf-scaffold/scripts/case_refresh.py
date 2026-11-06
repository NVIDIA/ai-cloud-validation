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
"""A reviewed case, written again from the current derivation with its answers kept.

A case holds what `instantiate` derives and what a person answered, and the
case's `judged` list names the second kind. A refresh derives the case again
and carries each judged value into it, so a kit correction reaches the case and
no answer is lost in silence. `check` and `render` run the same refresh in
memory: a case that it would change is stale.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: What an unfilled field holds. A judged path that holds one has nothing to carry.
EMPTY: tuple[Any, ...] = (None, {}, [], "")

#: The backup directory's prefix, so that a person who lists the temporary
#: directory can tell it from the others.
BACKUP_PREFIX = "tfkit-cases-"

#: How many differing paths a refusal names. A kit update can change twenty
#: fields of one case, and a message that lists all of them hides the command
#: that fixes them.
SHOWN_PATHS = 6

#: `expected_requests` is a list, and a judged path names an entry by its verb.
REQUESTS = "expected_requests"

#: Fields that the comparison of a refresh does not list one by one: `review`
#: is the contract's guidance, which no person writes, `judged` is the list of
#: answers itself, and `derived_from` is the record that each refresh writes.
NOT_LISTED = ("review", "judged", "derived_from")

#: The top-level fields that only a person writes, by contract. `instantiate`
#: writes some other fields only for some inputs, such as `narrowed` when it
#: drops a step, so a field that a new derivation lacks can still be the kit's.
#: Only a field of this list holds an answer that a refresh would lose.
PERSON_FIELDS: dict[str, tuple[str, ...]] = {
    "resource": ("dependencies", "peer", "not_exercised"),
    "module": ("differs_from_resource_case",),
}


def person_fields(case: dict[str, Any]) -> tuple[str, ...]:
    """The top-level fields of the case's contract that only a person writes."""
    return PERSON_FIELDS.get(str(case.get("contract")), ())


class RefreshError(ValueError):
    """A case cannot be derived again, and the reason."""


# --- one value at a path ----------------------------------------------------


def reach(case: dict[str, Any], path: str) -> Any:
    """One value of the case, by a dotted path.

    `expected_requests` is a list, so a path names an entry by its verb:
    `expected_requests.create.headers`.
    """
    here: Any = case
    for segment in path.split("."):
        if isinstance(here, list):
            here = next((e for e in here if isinstance(e, dict) and e.get("verb") == segment), None)
            continue
        if not isinstance(here, dict):
            return None
        here = here.get(segment)
    return here


def put(case: dict[str, Any], path: str, value: Any) -> bool:
    """Write `value` at a dotted path, and say whether the case had a place for it.

    A mapping on the way is created. A list entry is not: a verb that the
    derivation no longer has is an operation that the case no longer runs.
    """
    *parents, leaf = path.split(".")
    here: Any = case
    for segment in parents:
        if isinstance(here, list):
            here = next((e for e in here if isinstance(e, dict) and e.get("verb") == segment), None)
            if here is None:
                return False
            continue
        if not isinstance(here, dict):
            return False
        here = here.setdefault(segment, {})
    if not isinstance(here, dict):
        return False
    here[leaf] = value
    return True


def leaves(case: dict[str, Any]) -> dict[str, Any]:
    """Each value of the case by its dotted path, the expected requests by verb."""
    found: dict[str, Any] = {}

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict) and node:
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else str(key))
        elif prefix == REQUESTS and isinstance(node, list) and all(isinstance(e, dict) and e.get("verb") for e in node):
            for entry in node:
                walk({k: v for k, v in entry.items() if k != "verb"}, f"{prefix}.{entry['verb']}")
        else:
            found[prefix] = node

    walk(case, "")
    return found


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(f"{root}.") for root in roots)


# --- what a case was derived from ---------------------------------------------


def origin(
    coverage: Path,
    specs: Sequence[Path],
    cwd: Path,
    account_scope: str = "",
    provider_schema: Path | None = None,
    provider_data: Path | None = None,
) -> dict[str, Any]:
    """The `derived_from` record: the coverage, the documents a run named, its
    account scope, and the provider's reviewed schema and per-resource data
    that it read.

    Relative to the working directory, which is the repository root when an
    agent runs the script there, so the record holds in each clone. The scope
    changes what the derivation finds, so a second derivation needs it too.
    """

    def shown(path: Path) -> str:
        whole = path.resolve()
        try:
            return str(whole.relative_to(cwd.resolve()))
        except ValueError:
            return str(whole)

    record: dict[str, Any] = {"coverage": shown(coverage)}
    if specs:
        record["specs"] = [shown(path) for path in specs]
    if account_scope:
        record["account_scope"] = account_scope
    if provider_schema:
        record["provider_schema"] = shown(provider_schema)
    if provider_data:
        record["provider_data"] = shown(provider_data)
    return record


def with_origin(case: dict[str, Any], record: Any) -> dict[str, Any]:
    """The case with `derived_from` beside its contract, where a reader looks first."""
    placed: dict[str, Any] = {}
    for key, value in case.items():
        if key != "derived_from":
            placed[key] = value
        if key == "contract":
            placed["derived_from"] = record
    return placed


def recorded_inputs(
    path: Path,
    case: dict[str, Any],
    cwd: Path,
) -> tuple[Path, tuple[Path, ...], str, Path | None, Path | None]:
    """The coverage, the named documents, the provider schema and the provider
    data that the case records, found on disk, and its account scope."""
    record = case.get("derived_from")
    if not isinstance(record, dict) or not record.get("coverage"):
        raise RefreshError("the case records no `derived_from`, so nothing names the documents it was derived from")
    coverage = _found(str(record["coverage"]), path, cwd)
    specs = tuple(_found(str(name), path, cwd) for name in record.get("specs") or ())
    reviewed = _found(str(record["provider_schema"]), path, cwd) if record.get("provider_schema") else None
    data = _found(str(record["provider_data"]), path, cwd) if record.get("provider_data") else None
    return coverage, specs, str(record.get("account_scope") or ""), reviewed, data


def _found(name: str, case_path: Path, cwd: Path) -> Path:
    """A recorded path, from each directory above the case and then the working directory."""
    for base in [*case_path.absolute().parents, cwd]:
        candidate = base / name
        if candidate.is_file():
            return candidate
    raise RefreshError(
        f"{name}, which the case records in `derived_from`, is not found above {case_path.parent} nor in {cwd}"
    )


def subject_of(case: dict[str, Any]) -> tuple[str, str]:
    """What a case is derived for: a resource type, a layer, or the blueprint."""
    contract = case.get("contract")
    if contract == "module":
        return str(case.get("module") or ""), ""
    if contract == "composition":
        return "", ""
    resource = case.get("resource") or {}
    return (str(resource.get("terraform_address") or "").partition(".")[0], str(resource.get("case_name") or ""))


# --- the carry --------------------------------------------------------------


@dataclass
class Report:
    """What one refresh did with each case. Machine output, for the agent to show."""

    backup: str = ""
    carried: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    lost: list[dict[str, Any]] = field(default_factory=list)
    differs: list[dict[str, Any]] = field(default_factory=list)
    kept: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup": self.backup,
            "carried": self.carried,
            "refused": self.refused,
            "lost": self.lost,
            "differs": self.differs,
            "kept": self.kept,
        }


def carry(
    old: dict[str, Any],
    fresh: dict[str, Any],
    case_fields: Sequence[str],
    report: Report,
    name: str,
    *,
    keep_answers: bool = True,
) -> dict[str, Any]:
    """The new case: `fresh` with each judged value of `old` that still has a field.

    A judged value whose top-level field the contract no longer lists, or whose
    operation the derivation no longer has, is lost and listed. With
    `keep_answers` off every judged value is lost, as `--overwrite` asks.
    """
    new = copy.deepcopy(fresh)
    carried: list[str] = []
    for path in [str(p) for p in old.get("judged") or []]:
        value = reach(old, path)
        if value in EMPTY:
            continue
        reason = (
            "--overwrite discards it"
            if not keep_answers
            else "the contract no longer lists this field"
            if path.split(".")[0] not in case_fields
            else ""
        )
        if not reason and not put(new, path, copy.deepcopy(value)):
            reason = "the new case has no place for it"
        if reason:
            report.lost.append({"case": name, "path": path, "value": value, "reason": reason})
            continue
        carried.append(path)
        report.carried.append({"case": name, "path": path})
    listed = list(fresh.get("judged") or [])
    kept = [p for p in old.get("judged") or [] if p in listed or p in carried]
    new["judged"] = kept + [p for p in listed if p not in kept]
    _differs(old, fresh, carried, report, name)
    return new


def _differs(
    old: dict[str, Any],
    fresh: dict[str, Any],
    carried: list[str],
    report: Report,
    name: str,
) -> None:
    """Each field that the backup holds and the new derivation sets otherwise.

    A field that only the new derivation holds carries no answer to keep, so
    it is no decision for the operator.
    """
    judged = [str(p) for p in old.get("judged") or []]
    before, derived = leaves(old), leaves(fresh)
    for path in sorted(before):
        if _under(path, NOT_LISTED) or _under(path, judged):
            continue
        if before.get(path, ...) != derived.get(path, ...):
            report.differs.append(_difference(name, path, before, derived))
    for path in carried:
        value = reach(fresh, path)
        if value not in EMPTY and value != reach(old, path):
            report.differs.append(
                {"case": name, "path": path, "backup": reach(old, path), "derived": value, "judged": True}
            )


def _difference(name: str, path: str, before: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"case": name, "path": path}
    if path in before:
        entry["backup"] = before[path]
    if path in derived:
        entry["derived"] = derived[path]
    return entry


def refusals(
    new: dict[str, Any],
    fresh: dict[str, Any],
    check: Callable[[dict[str, Any]], list[str]],
    report: Report,
    name: str,
) -> None:
    """Each refusal of a current check that a carried value causes.

    The bare derivation is checked too: its refusals are the unfilled fields,
    which a carried value removes and never causes.
    """
    bare = set(check(fresh))
    for message in check(new):
        if message not in bare:
            report.refused.append({"case": name, "message": message})


# --- a stale case -----------------------------------------------------------


def staleness(
    path: Path,
    case: dict[str, Any],
    derive: Callable[[Path, dict[str, Any]], dict[str, Any]],
    case_fields: Sequence[str],
) -> list[str]:
    """Why the case is not what the current kit writes for it, empty when it is.

    A case is stale when a refresh would change it. So a judged value is never
    stale: the refresh carries it.
    """
    try:
        fresh = derive(path, case)
    except RefreshError as error:
        return [f"stale: {error}. {_REFRESH}"]
    new = carry(case, fresh, case_fields, Report(), path.name)
    if new == case:
        return []
    changed = _changed(leaves(case), leaves(new))
    dropped = sorted((set(case) - set(new)) & set(person_fields(case)))
    shown = ", ".join(changed[:SHOWN_PATHS]) + (
        f" and {len(changed) - SHOWN_PATHS} more" if len(changed) > SHOWN_PATHS else ""
    )
    message = f"stale: the current kit derives this case differently at {shown}. {_REFRESH}"
    if dropped:
        message += (
            f" The refresh drops {', '.join(dropped)}, because `judged` does not name "
            "it: when a person wrote it, add it to `judged` first."
        )
    return [message]


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Each differing path, cut at the first part that one side does not hold.

    An operation that one side lacks differs at a dozen leaves; its name says it.
    """

    def prefixes(paths: dict[str, Any]) -> set[str]:
        return {".".join(p.split(".")[:n]) for p in paths for n in range(1, p.count(".") + 2)}

    held = prefixes(before) & prefixes(after)
    shown: dict[str, None] = {}
    for path in sorted(set(before) | set(after)):
        if before.get(path, ...) == after.get(path, ...):
            continue
        parts = path.split(".")
        cut = next((n for n in range(1, len(parts) + 1) if ".".join(parts[:n]) not in held), len(parts))
        shown[".".join(parts[:cut])] = None
    return list(shown)


_REFRESH = (
    "Refresh the case: run `instantiate` with its `contract`, the coverage that its "
    "`derived_from` names, `--out` this directory and a scope that selects the case. It "
    "keeps each judged answer, and prints a report of what it carried, refused, lost and "
    "found different. Show the operator that report."
)


# --- the backup -------------------------------------------------------------


def back_up(out: Path, root: Path | None) -> Path:
    """A copy of the cases, outside them, in a new directory for this run.

    Outside, because `check`, `render` and `progress` read every YAML file under
    the cases as a case. `root` is the system temporary directory unless a test
    names another.
    """
    target = Path(tempfile.mkdtemp(prefix=BACKUP_PREFIX, dir=root)) / out.name
    shutil.copytree(out, target)
    return target
