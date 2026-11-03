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
"""The provider's own settings, for `.env.example`, read from its schema export.

A generated test configuration has no `provider` block, so a test configures
the provider through the environment alone, and each setting's description
names the variable it falls back to. `ncp-tf-provider` writes the export into
the provider's directory. A provider render finds it there, in its own
`--runner-out`, on every run. A module or blueprint render is given the path,
because its own `.env` configures the same provider.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any

EXPORT_NAME = "provider-schema-export.json"
#: An environment variable, and the phrase a description states its fallbacks
#: in, first one first. `ncp-tf-provider`'s `settings` check reads the same
#: phrase, so the example sets exactly the variable that check accepts, and not
#: one the description only mentions.
ENVIRONMENT_VARIABLE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
FALLBACK = re.compile(
    r"Falls back to ("
    + ENVIRONMENT_VARIABLE.pattern
    + r"(?:(?:,\s*or\s+|,\s*|\s+or\s+)"
    + ENVIRONMENT_VARIABLE.pattern
    + r")*)"
)
#: The comment width of the rest of `.env.example`.
WIDTH = 76


def settings(export: dict[str, Any]) -> list[tuple[str, str]]:
    """Each optional provider setting's first variable, with its description.

    The first variable the fallback phrase names is the one this file sets;
    the description keeps the others, in the provider's own words.
    """
    found: list[tuple[str, str]] = []
    for _, schema in sorted((export.get("provider_schemas") or {}).items()):
        block = ((schema or {}).get("provider") or {}).get("block") or {}
        for _, attribute in sorted((block.get("attributes") or {}).items()):
            description = str((attribute or {}).get("description") or "")
            stated = FALLBACK.search(description)
            names = ENVIRONMENT_VARIABLE.findall(stated.group(1)) if stated else []
            if (attribute or {}).get("optional") and names:
                found.append((names[0], description.strip()))
    return found


def section(export: Path) -> list[str]:
    """The `.env.example` lines for the provider's settings, read from `export`."""
    lines = ["# ---- the provider's own settings ----", "#"]
    if not export.is_file():
        return [
            *lines,
            f"# None yet: the render found no {EXPORT_NAME}. Render again after",
            "# the provider exports its schema, and they appear here.",
            "",
        ]
    found = settings(json.loads(export.read_text(encoding="utf-8")))
    if not found:
        return [*lines, f"# {EXPORT_NAME} names no setting with a variable.", ""]
    lines += [
        "# These configure the provider rather than a case. Each one is an",
        "# attribute of the provider block, and its description below is the",
        "# provider's own.",
        "",
    ]
    for name, description in found:
        lines += [f"# {line}" for line in textwrap.wrap(description, WIDTH)]
        lines += [f"{name}=", ""]
    return lines
