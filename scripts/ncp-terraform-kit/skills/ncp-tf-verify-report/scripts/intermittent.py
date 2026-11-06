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
"""A blocker that one passing run does not clear.

A pass drops a resource's citations, because the run just disproved them. An
intermittent defect is the exception: the run that passed is the same evidence
as the run that failed. A blocker marked `intermittent: true` stays cited
through a pass, and leaves the record only when a person deletes it, in the
commit that names the cause found and fixed.
"""

from __future__ import annotations

from typing import Any

FIELD = "intermittent"


def ids(blockers: list[Any]) -> set[str]:
    """The ids of the blockers a pass does not clear."""
    return {str(b.get("id")) for b in blockers if isinstance(b, dict) and b.get(FIELD) is True}


def after_a_pass(entry: dict[str, Any], kept: set[str]) -> None:
    """Drop every citation a pass disproves, and keep the intermittent ones."""
    cited = [str(i) for i in entry.get("blocked_by") or [] if str(i) in kept]
    if cited:
        entry["blocked_by"] = cited
    else:
        entry.pop("blocked_by", None)


def problem(blocker: dict[str, Any]) -> str | None:
    if FIELD in blocker and not isinstance(blocker[FIELD], bool):
        return f"{FIELD} is {blocker[FIELD]!r}; it is true or false"
    return None


def label(blocker: dict[str, Any]) -> str:
    """The category as the report shows it."""
    category = f"`{blocker.get('category')}`"
    return f"{category}, intermittent" if blocker.get(FIELD) is True else category
