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
"""Where a supplied dependency name goes: where the created one's name stands.

A run can name an existing dependency resource instead of creating one. Each
case refers to that resource in the form its own API takes -- a path for one,
a full URL for another -- so one resource has several forms across cases, and
one run value serves them all. The run supplies the name, and each test puts
it where its case's `reference` holds the name of the resource it would create.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

#: A placeholder in a reference: `{project_id}`, `{run_id}`.
PLACEHOLDER = r"\{([a-z_][a-z0-9_]*)\}"

Formatter = Callable[[str, str], tuple[str, list[str]]]


def builds_reference(entry: dict[str, Any], created: str) -> bool:
    """Whether a supplied value is a name the test builds this case's reference around.

    An `attribute` dependency takes a value the API assigned, such as an
    address, and uses it as it is. `check` refuses a reference that does not
    hold the created name, so every other dependency has a place for it.
    """
    return not entry.get("attribute") and created in str(entry.get("reference") or "")


def go_value(entry: dict[str, Any], created: str, formatted: Formatter, *, checked: bool = True, form: str = "") -> str:
    """The Go expression the live test stores for a supplied value held in `value`.

    `checked` refuses a value that is not a name, where an operator sets it, and
    an `attribute` value that does not match `form`, the pattern its field states.
    """
    if not builds_reference(entry, created):
        if checked and form:
            return f"suppliedForm(t, {json.dumps('TFKIT_VAR_' + entry['variable'].upper())}, value, {json.dumps(form)})"
        return "value"
    variable = entry["variable"]
    named = f"suppliedName(t, {json.dumps('TFKIT_VAR_' + variable.upper())}, value)" if checked else "value"
    own = f"supplied[{json.dumps(variable)}]"
    reference = str(entry["reference"]).replace(created, "{" + variable + "}")
    text, arguments = formatted(reference.replace("%", "%%"), PLACEHOLDER)
    arguments = [named if argument == own else argument for argument in arguments]
    if text == "%s" and len(arguments) == 1:
        return arguments[0]
    return f"fmt.Sprintf({json.dumps(text)}, {', '.join(arguments)})"


def replay_line(entry: dict[str, Any], created: str, formatted: Formatter) -> str:
    """The replay test's line that builds a supplied dependency's form from its name.

    The recording holds the name that the run supplied, so the replayer fills
    the recording with the name, and the configuration takes the form that the
    live test builds around it. The replay test has no run id of its own; it
    sends `stubRunID`. `record` refuses a supplied path, so the recorded value
    is a name, and replay does not check it again.
    """
    if not builds_reference(entry, created):
        return ""

    def replayed(text: str, pattern: str) -> tuple[str, list[str]]:
        text, arguments = formatted(text, pattern)
        return text, ["stubRunID" if argument == "id" else argument for argument in arguments]

    own = f"supplied[{json.dumps(entry['variable'])}]"
    return (
        f'\tif value := {own}; value != "" {{\n\t\t{own} = {go_value(entry, created, replayed, checked=False)}\n\t}}\n'
    )
