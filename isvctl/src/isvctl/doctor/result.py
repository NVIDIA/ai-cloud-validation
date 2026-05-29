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

"""Result types for doctor checks."""

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    """Status of a single check or a category as a whole."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


# Worst-status ranking: FAIL beats WARN beats OK beats SKIP.
_RANK: dict[Status, int] = {
    Status.SKIP: 0,
    Status.OK: 1,
    Status.WARN: 2,
    Status.FAIL: 3,
}


def worst(statuses: list[Status]) -> Status:
    """Return the worst (most severe) status from a list.

    Empty list collapses to SKIP so an empty category renders neutrally.
    """
    if not statuses:
        return Status.SKIP
    return max(statuses, key=lambda s: _RANK[s])


@dataclass(frozen=True)
class CheckResult:
    """One row in a category report.

    Attributes:
        name: short identifier of the thing being checked (e.g. "terraform",
            "ISV_CLIENT_ID").
        status: OK / WARN / FAIL / SKIP.
        message: one-line summary suitable for terminal display.
        detail: verbose-mode-only extra info (path, version, traceback excerpt).
        remediation: short user-facing hint on how to fix a FAIL/WARN.
        group: optional sub-group label for rendering (e.g. "ISV Lab Service").
    """

    name: str
    status: Status
    message: str = ""
    detail: str | None = None
    remediation: str | None = None
    group: str | None = None


@dataclass
class CategoryReport:
    """A category of related checks (tools, env, configs)."""

    name: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def worst_status(self) -> Status:
        """Worst status across all results."""
        return worst([r.status for r in self.results])

    def counts(self) -> dict[Status, int]:
        """Return per-status counts."""
        out: dict[Status, int] = {s: 0 for s in Status}
        for r in self.results:
            out[r.status] += 1
        return out
