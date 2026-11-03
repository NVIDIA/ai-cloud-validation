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
"""What the cases that `instantiate` added owe a reviewer, printed on stderr."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def report(written: list[dict[str, Any]], out: Path, *, selection_file: str, left_out: int) -> None:
    """The decisions the added cases owe. A refreshed case is in the refresh report.

    `selection_file` is named only after a resource run, the one run that can
    cover part of a plan, and `left_out` counts the resources it has no case for.
    """
    owed: dict[str, int] = {}
    for case in written:
        for path in case.get("judged") or []:
            owed[str(path)] = owed.get(str(path), 0) + 1
    if not written:
        return
    tally = "".join(f"\n  {name:<16} {count} of {len(written)} case(s)" for name, count in sorted(owed.items()))
    # A field owed by fewer cases than were written reads as cases somebody
    # failed to finish. It is the opposite: the API offers no such operation, so
    # the step and the decision are both gone, and the case says so.
    short = (
        "\nA field owed by fewer cases is not an omission. The API offers no such "
        "operation there, so the step and its decision are both gone; each such "
        "case names what it dropped under `narrowed`."
        if any(count < len(written) for count in owed.values())
        else ""
    )
    # The agent reads this text just before it reports. A summary that names
    # nobody who fills the fields reads as decisions for the operator. Each live
    # create costs some money, so cost cannot decide who fills a value: the agent
    # fills it, and the operator decides spending at the live run.
    print(
        f"\n{len(written)} case(s) in {out}, with {sum(owed.values())} judged fields "
        f"empty:{tally}{short}\n"
        f"Next: the agent fills each judged field except `readiness` now, before it "
        f"reports. Take each value from the API documents and the approved plan. "
        f"Each case lists these fields under `review`: the question, what a wrong answer "
        f"costs, the shape to write and an example. Write the answer at the field "
        f"the entry names, not under `review`, which nothing reads back. A fixture "
        f"value that names another resource is declared under `dependencies`. "
        f"Change nothing else in a case. "
        f"Fill each `fixture.create` and `fixture.update` also when the resource costs "
        f"money: write the smallest value that serves the case. "
        f"Then show the operator the values, and what each case creates that costs money. "
        f"Ask the operator only for the readiness and a second case of one type. A cost "
        f"is never a reason to leave a field empty: the operator decides spending when "
        f"they choose the resources of a live run."
        + (f"\n{selection_file} records which planned resources have no case here." if selection_file else ""),
        file=sys.stderr,
    )
    if left_out:
        print(
            f"{left_out} resources have no case in {out}. {selection_file} names each one and why; it is not a pass.",
            file=sys.stderr,
        )
