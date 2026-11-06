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
"""One dependency resource in the form that each fixture of a workspace takes.

A case and a dependency resource that it creates can both use one resource,
each in the form its own API takes: a cluster takes its network by name, and
the subnetwork it also needs takes the same network by path. The workspace
creates that resource once. The case's own form is the one that the run value
`TFKIT_VAR_<VARIABLE>` is built into. Every other form is built from the name
inside that value, because each form holds the created name once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def another_form(first: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Whether `entry` declares the dependency of `first` with only another `reference`."""
    if not (first.get("resource") and first.get("reference") and entry.get("reference")):
        return False
    return {**first, "reference": ""} == {**entry, "reference": ""}


def own_reference(consumer: dict[str, Any], variable: str) -> str:
    """The `reference` that `consumer` declares for `variable`, or an empty string."""
    for entry in consumer.get("dependencies") or []:
        if entry.get("variable") == variable and entry.get("reference"):
            return str(entry["reference"])
    return ""


def supplied_in(variable: str, primary: str, other: str, created: str, text: Callable[[str], str]) -> str:
    """The HCL that puts the name in a supplied `primary` form into the `other` form.

    `created` is the created name that both forms hold, and `text` writes a
    part of a form as an HCL string.
    """
    name = f"var.{variable}"
    before, _, after = primary.partition(created)
    if before:
        name = f"trimprefix({name}, {text(before)})"
    if after:
        name = f"trimsuffix({name}, {text(after)})"
    before, _, after = other.partition(created)
    parts = [*([text(before)] if before else []), name, *([text(after)] if after else [])]
    return f'join("", [{", ".join(parts)}])' if len(parts) > 1 else name
