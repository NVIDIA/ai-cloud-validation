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
"""One `instantiate` run over a directory that may hold reviewed cases.

Each subject is derived first, then the directory is backed up, then each case
is written: a new one as derived, an existing one with its judged answers
carried. The derivation and the writing are the caller's, so this module holds
the order and the report, and knows no contract.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import case_refresh
import yaml

#: One subject of a run: what is derived, the case's name, its file name, and
#: the directories under `--out` that a new case is filed in.
Subject = tuple[str, str, str, tuple[str, ...]]
#: A derived case, where it goes, and whether a case is there already.
Built = tuple[Path, dict[str, Any], bool]


def derive_all(
    subjects: Sequence[Subject],
    derive: Callable[[str, str], dict[str, Any]],
    existing: dict[tuple[str, str], Path],
    out: Path,
    report: case_refresh.Report,
    refusal: type[Exception],
) -> list[Built]:
    """Each subject's new case, before any case is written.

    A new subject that does not build stops the run with the directory as it
    was. An existing case that no longer builds is kept as it is, and listed.
    """
    built: list[Built] = []
    for subject, name, stem, filed in subjects:
        there = existing.get((subject, name))
        try:
            fresh = derive(subject, name)
        except refusal as error:
            if there is None:
                raise
            report.kept.append({"case": str(there.relative_to(out)), "reason": str(error)})
            continue
        built.append((there or out.joinpath(*filed) / f"{stem}.yaml", fresh, there is not None))
    return built


def unjudged(
    built: Sequence[Built],
    out: Path,
    case_fields: Sequence[str],
    read: Callable[[Path], Any],
) -> list[str]:
    """Each field of an existing case that only a person writes, and `judged` does not name.

    The derivation writes no such field, so a refresh would drop the answer.
    Only the top-level fields are this certain: a derived field that differs
    can be a kit correction as well as a hand fix, and the report lists it. A
    derived field that the new derivation does not write is dropped and
    listed in the same way.
    """
    found = []
    for path, fresh, there in built:
        if not there:
            continue
        old = read(path)
        owned = {str(p).split(".")[0] for p in old.get("judged") or []}
        written = case_refresh.person_fields(old)
        fields = [key for key in old if key in case_fields and key in written and key not in fresh and key not in owned]
        if fields:
            found.append(f"{path.relative_to(out)}: {', '.join(fields)}")
    return found


def write_all(
    built: Sequence[Built],
    out: Path,
    report: case_refresh.Report,
    *,
    case_fields: Sequence[str],
    read: Callable[[Path], Any],
    write: Callable[[Path, dict[str, Any]], None],
    check: Callable[[dict[str, Any]], list[str]],
    overwrite: bool,
    backup_root: Path | None,
) -> list[dict[str, Any]]:
    """Write each case, and return the new ones.

    The backup comes before the first write over an existing case, and the
    report goes to stdout when the run refreshed or kept a case.
    """
    if any(there for _, _, there in built):
        report.backup = str(case_refresh.back_up(out, backup_root))
        print(f"The cases were copied to {report.backup} before this run wrote one.", file=sys.stderr)
    added = []
    for path, fresh, there in built:
        if not there:
            write(path, fresh)
            added.append(fresh)
            continue
        name = str(path.relative_to(out))
        new = case_refresh.carry(read(path), fresh, case_fields, report, name, keep_answers=not overwrite)
        case_refresh.refusals(new, fresh, check, report, name)
        write(path, new)
    if report.backup or report.kept:
        print(yaml.safe_dump(report.as_dict(), sort_keys=False, allow_unicode=True), end="")
        print(
            "The refresh report is on stdout. Show it to the operator, and go through each "
            "refused, lost and differing entry with them.",
            file=sys.stderr,
        )
    return added


def stale_cases(
    cases: Sequence[Path],
    loaded: Sequence[Any],
    stale: Callable[[Path, dict[str, Any]], list[str]],
) -> list[str]:
    """Each case of a directory that the current kit derives differently, with the reason."""
    return [
        f"{path.name}: {reason}"
        for path, case in zip(cases, loaded)
        if isinstance(case, dict) and case.get("kind") == "case"
        for reason in stale(path, case)
    ]
