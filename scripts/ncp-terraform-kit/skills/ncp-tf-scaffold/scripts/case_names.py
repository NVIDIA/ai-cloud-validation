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
"""The names that a run gives what it creates, and the checks each name owes.

The rendered test sends the create fixture as it is when the fixture holds the
run id in any value, name included. Only a fixture that holds none gets
`naming.name_pattern` in the field that `naming.name_field` names. So the name
that the API receives is a fixture value as often as it is the pattern, and a
check that reads only the pattern passes a name that no run sends.
"""

from __future__ import annotations

import re
from typing import Any

import attribute_names

#: A run's resources must be findable by the sweeper and by a person, without
#: touching anything the run did not create.
KIT_PREFIX = "tfkit"
RUN_ID = "{run_id}"
#: The shortest limit any target imposes wins, because the convention is one
#: convention. Lowercase letters, digits and hyphens, opening with a letter, is
#: what survives every naming rule the four layers meet.
NAME_MAX = 63
#: What the sweeper finds a run's resource by, in the last segment of its
#: name: the kit prefix, then the run id, directly or after one separator. An
#: API can require a fixed text before the kit prefix, as GCP requires
#: `custom.`. The sweep plan records that text, so it holds no placeholder.
RUN_NAME = re.compile(r"[^{}]*" + KIT_PREFIX + r"[^a-z0-9{]?" + re.escape(RUN_ID))
#: The length of the run id that the provider's entry point makes: `tfkit` and
#: eight base-36 characters. A name that leaves less room for it reaches the
#: API over its limit.
TOKEN_ROOM = 13


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _at(values: Any, path: str) -> Any:
    """The value at a dotted path, in the document's spelling or the fixture's."""
    for spelling in (path, attribute_names.configuration_path(path)):
        here = values
        for part in spelling.split("."):
            here = here.get(part) if isinstance(here, dict) else None
        if here is not None:
            return here
    return None


def sent(case: dict[str, Any]) -> list[str]:
    """Each name that the rendered test sends for the case's resource."""
    naming = case.get("naming") or {}
    create = (case.get("fixture") or {}).get("create")
    strings = _strings(create)
    if not any(RUN_ID in s for s in strings):
        pattern = naming.get("name_pattern")
        return [str(pattern)] if pattern and naming.get("name_field", "name") else []
    # A label value that is the run id alone names the run, not the resource.
    names = [s for s in strings if RUN_ID in s and s != RUN_ID]
    field = str(naming.get("name_field", "name") or "")
    named = _at(create, field) if field else None
    if isinstance(named, str) and named not in names:
        names.append(named)
    return names


def refusals(case: dict[str, Any]) -> list[str]:
    """Each name the test sends that the sweeper cannot find or the API refuses."""
    naming = case.get("naming") or {}
    limit = min(int(naming.get("name_limit") or NAME_MAX), NAME_MAX)
    broken = []
    for sent_name in sent(case):
        # A name sent as a path names the resource in its last segment, and
        # its scope in the segments before it.
        name = sent_name.rpartition("/")[2]
        if name != sent_name:
            name_in = f"the test sends the name {name!r} in {sent_name!r}"
        else:
            name_in = f"the test sends the name {name!r}"
        if RUN_ID not in name:
            broken.append(
                f"{name_in}, which carries no run id; two runs "
                "create the same name, and the sweeper cannot tell them apart"
            )
            continue
        if not RUN_NAME.match(name):
            broken.append(
                f"{name_in}, which does not open with the kit's prefix and the run id, "
                "after any prefix that the API requires"
            )
        length = len(name) - len(RUN_ID) + TOKEN_ROOM
        if length > limit:
            broken.append(f"{name_in}, which reaches {length} characters with a run id, and this API allows {limit}")
    return broken
