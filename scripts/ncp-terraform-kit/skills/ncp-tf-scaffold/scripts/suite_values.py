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
"""What the layer module and blueprint tests read from a run, listed before it.

Each module directory has its own `.env`, so the values a module's live test
reads are listed in that directory: in `RUN_VARIABLES.md` beside the tests, and
in the module's `.env.example`. Both come from the live test files on disk, the
files the module's runner checks, so whichever render runs last writes the same
files.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: How a rendered test reads a run value. The runner finds the unset ones with
#: the same pattern, so the example and the refusal name the same set.
LIVE_VALUE = re.compile(r"var\.tfkit_var_([a-z0-9_]+)")


#: A string that is one interpolation and nothing else.
ONLY_A_REFERENCE = re.compile(r"\$\{(var\.[a-z0-9_]+)\}")


def bare_references(value: Any, expression: Callable[[str], Any]) -> Any:
    """`value` with each string that is only a reference written as the reference.

    `terraform fmt` rewrites `"${var.x}"` to `var.x`, so a rendered file that
    keeps the quoted form fails `terraform fmt -check`.
    """
    if isinstance(value, str):
        whole = ONLY_A_REFERENCE.fullmatch(value)
        return expression(whole.group(1)) if whole else value
    if isinstance(value, dict):
        return {k: bare_references(v, expression) for k, v in value.items()}
    if isinstance(value, list):
        return [bare_references(v, expression) for v in value]
    return value


def module_rows(
    cases: list[dict[str, Any]],
    supplied: Callable[[Any], list[str]],
    context: Callable[[Any, str], tuple[str, str] | None],
) -> list[tuple[str, str, str]]:
    """Each run value a module or blueprint test that applies reads, and where it goes."""
    rows: list[tuple[str, str, str]] = []
    for case in cases:
        if "apply_create" not in (case.get("lifecycle") or []):
            continue
        variables = case.get("variables") or {}
        for name in supplied(variables):
            where = context(variables, name)
            goes = f"`{where[0]}`, as `{where[1]}`" if where else "the variables"
            subject = case.get("module") or "blueprint"
            rows.append((str(subject), f"TFKIT_VAR_{name.upper()}", goes))
    return sorted(rows)


def render_module_run_variables(rows: list[tuple[str, str, str]]) -> str:
    return "\n".join(
        [
            "# What a live run of these tests has to supply",
            "",
            "Each value names something only the account knows, or a resource that must",
            "exist before the test runs. The `tools/run-tests.sh live` of the module or",
            "blueprint these tests belong to reads them from the `.env` in its directory,",
            "and a run missing one stops before it creates anything.",
            "",
            "The offline tests read none of these.",
            "",
            "This file is generated with the cases. Editing it changes nothing.",
            "",
            "| module | variable | goes into |",
            "|---|---|---|",
            *(f"| `{module}` | `{variable}` | {goes} |" for module, variable, goes in rows),
            "",
        ]
    )


def needed(tests: Path) -> list[str]:
    """Each `TFKIT_VAR_` value a live test file in `tests` reads, sorted."""
    return sorted(
        {
            f"TFKIT_VAR_{name.upper()}"
            for test in tests.glob("*_live.tftest.hcl")
            for name in LIVE_VALUE.findall(test.read_text(encoding="utf-8"))
        }
    )
