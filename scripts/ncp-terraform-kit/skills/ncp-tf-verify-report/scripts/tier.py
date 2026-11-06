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
"""Which tier of run set a status.

A failed live lifecycle and a passed offline test both record `offline`. A
later offline pass shows less than the first and as much as the second, so it
may replace only the second. The status cannot tell them apart, and the
evidence is prose for a person, so each entry a run sets names its tier.
"""

from __future__ import annotations

from typing import Any

FIELD = "tier"
#: `live`: a lifecycle result or a live module test set the status.
#: `offline`: an offline or replay result set it.
TIERS = ("live", "offline")
#: A run's output is not committed, so a run id or a run date names nothing a
#: reader can open. Git history says when each line of the record changed.
RUN_KEYS = ("run_id", "recorded")


def keeps(entry: dict[str, Any]) -> bool:
    """A later offline pass leaves this entry as it is."""
    return entry.get(FIELD) == "live" and entry.get("status") in {"live", "offline"}


def named_runs(record: dict[str, Any]) -> list[str]:
    """Each place in the record that names a run, as `<where> names <key>`."""
    found = []
    stamp = record.get("stamp")
    if isinstance(stamp, dict):
        found += [f"stamp names {key}" for key in RUN_KEYS if key in stamp]
    for label, entries in (("", record.get("resources")), ("module ", record.get("modules"))):
        for name, entry in sorted((entries if isinstance(entries, dict) else {}).items()):
            if isinstance(entry, dict):
                found += [f"{label}{name} names {key}" for key in RUN_KEYS if key in entry]
    return found


def untiered(record: dict[str, Any]) -> list[str]:
    """Each `live` or `offline` entry that names no known tier.

    `record` refuses such a record rather than guess. A default of `offline`
    lets an offline pass replace a live failure that owes a reason.
    """
    found = []
    for label, entries in (("", record.get("resources")), ("module ", record.get("modules"))):
        for name, entry in sorted((entries if isinstance(entries, dict) else {}).items()):
            if isinstance(entry, dict) and entry.get("status") in {"live", "offline"} and entry.get(FIELD) not in TIERS:
                found.append(f"{label}{name}")
    return found
