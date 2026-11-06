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
"""The operator's reason for each planned type that a resource run leaves out.

A requirement needs each planned type: a dependency resource is planned only
because a requirement resource needs it. So a type the operator chose not to
build owes `why` and `source`. No script knows either, so the kit writes them
empty, keeps the operator's answer when it writes the file again, and `check`
refuses an empty one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

NOT_SELECTED = "not_selected"
#: The operator's reason, and who or what decided: an operator decision, a
#: document, a release note.
FIELDS = ("why", "source")


def with_answers(skipped: list[dict[str, Any]], previous: Path) -> list[dict[str, Any]]:
    """Each `not_selected` entry with `why` and `source`, as the earlier file gave them.

    `instantiate` writes the file again on each run, so an answer that only the
    earlier file holds would be lost.
    """
    answered = _answers(previous)
    for entry in skipped:
        if entry.get("reason") == NOT_SELECTED:
            kept = answered.get(str(entry.get("resource")), {})
            entry.update({name: kept.get(name, "") for name in FIELDS})
    return skipped


def refusals(selection: dict[str, Any]) -> list[str]:
    """Why `check` fails the selection: each `not_selected` type with no reason."""
    return [
        f"{entry.get('resource')} is not_selected and gives no {' or '.join(missing)}. "
        "A requirement needs it, so ask the operator why it is not built and who or "
        "what decided (an operator decision, a document, a release note). Write the "
        "answer in `why` and `source`; do not write a reason the operator did not give. "
        "The scope of the request is not a reason."
        for entry in selection.get("resources_skipped") or []
        if isinstance(entry, dict)
        and entry.get("reason") == NOT_SELECTED
        and (missing := [f"`{name}`" for name in FIELDS if not str(entry.get(name) or "").strip()])
    ]


def _answers(previous: Path) -> dict[str, dict[str, Any]]:
    if not previous.is_file():
        return {}
    record = yaml.safe_load(previous.read_text(encoding="utf-8")) or {}
    return {
        str(entry.get("resource")): entry
        for entry in record.get("resources_skipped") or []
        if isinstance(entry, dict) and entry.get("reason") == NOT_SELECTED
    }
