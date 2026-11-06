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
"""Whether every provider setting has an environment fallback.

Reads the provider's own `terraform providers schema -json` output. Exit 1 is
a finding about the provider, so an input this cannot read is refused as
`UnreadableExport` and never reaches that code.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

#: An environment variable, as a description has to spell one for this check to
#: see it. Terraform's schema carries no field for a fallback, so the only place
#: one can be stated machine-readably is the description the provider writes.
ENVIRONMENT_VARIABLE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
#: The phrase that states the fallbacks, first one first. A variable the
#: description only mentions is not a fallback, and `.env.example` sets the
#: first variable this phrase names.
FALLBACK = re.compile(
    r"Falls back to ("
    + ENVIRONMENT_VARIABLE.pattern
    + r"(?:(?:,\s*or\s+|,\s*|\s+or\s+)"
    + ENVIRONMENT_VARIABLE.pattern
    + r")*)"
)


class UnreadableExport(Exception):
    """The export is missing, is not JSON, or is not a provider schema export."""


def read_export(path: Path) -> dict[str, Any]:
    """The export at `path`, or `UnreadableExport` naming the path and why.

    A JSON object without `provider_schemas` has no settings to check, so it
    would pass. The derivation `provider-schema.json` is such a file, and it is
    the one an agent passes by mistake.
    """
    try:
        export = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise UnreadableExport(f"no such file: {path}") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise UnreadableExport(f"{path} is not valid JSON: {error}") from error
    if not isinstance(export, dict) or not isinstance(export.get("provider_schemas"), dict):
        raise UnreadableExport(
            f"{path} is not a provider schema export: it has no `provider_schemas` "
            "object. Pass the output of `terraform providers schema -json`"
        )
    return export


def settings_without_a_fallback(export: dict[str, Any]) -> list[str]:
    """Provider-block attributes whose description names no environment variable.

    A generated test configuration carries no `provider` block, so the
    environment is the only way a test can configure the provider at all. An
    attribute with no fallback cannot be set by any test, and whatever it
    controls is unreachable from the one place that would prove it works.

    This reads descriptions, which is where a fallback can be stated and not
    where it is implemented. It cannot see a fallback the code has and the
    description omits -- and that is the same defect from the operator's side,
    because nothing then tells the operator the variable exists.
    """
    broken: list[str] = []
    for address, schema in sorted((export.get("provider_schemas") or {}).items()):
        block = ((schema or {}).get("provider") or {}).get("block") or {}
        for name, attribute in sorted((block.get("attributes") or {}).items()):
            if not isinstance(attribute, dict) or not attribute.get("optional"):
                continue
            if FALLBACK.search(str(attribute.get("description") or "")):
                continue
            broken.append(
                f"{address}: `{name}` is optional and its description states no "
                "`Falls back to <VARIABLE>`, so no generated test can set it"
            )
    return broken


def check(path: Path) -> int:
    """0 when every setting has a fallback, 1 when one has none."""
    broken = settings_without_a_fallback(read_export(path))
    for line in broken:
        print(line, file=sys.stderr)
    if broken:
        print(
            "\nEvery provider setting needs an environment fallback, and a switch "
            "needs one most: a capability no test can turn on is one nothing "
            "proves. State the variable in the attribute's description.",
            file=sys.stderr,
        )
        return 1
    print("settings: ok")
    return 0
