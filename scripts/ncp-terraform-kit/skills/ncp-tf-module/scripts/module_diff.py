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
"""What a newer schema export changes in one layer's module, before `derive` runs.

It compares what `derive` puts in each variable's object type: the settable
attributes and the `timeouts` field. Anything else in the export does not
reach the module, so a change to it is not reported.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: (terraform_type, resource schema) -> (settable fields, the timeouts field or None).
Derive = Callable[[str, dict[str, Any]], tuple[list[Any], Any]]


@dataclass
class Report:
    #: Lines for stdout: each change, under the resource type it is in.
    changes: list[str] = field(default_factory=list)
    #: Lines for stderr: planned types that are in neither export.
    notes: list[str] = field(default_factory=list)


def _variable_fields(derive: Derive, terraform_type: str, schema: dict[str, Any]) -> dict[str, str]:
    fields, timeouts = derive(terraform_type, schema)
    return {f.name: f.hcl_type for f in [*fields, *([timeouts] if timeouts else [])]}


def compare(
    resources: list[dict[str, Any]],
    old: dict[str, Any],
    new: dict[str, Any],
    derive: Derive,
) -> Report:
    """Each planned type's change between the `old` and `new` resource schemas."""
    report = Report()
    for r in resources:
        tt = r["terraform_type"]
        if tt not in old and tt not in new:
            # The provider does not implement it in either export, so the
            # module wraps it in neither: nothing changes for it.
            report.notes.append(f"note: {tt} is in neither export; not compared")
            continue
        if tt not in old:
            report.changes.append(f"{tt}: added in the new export")
            continue
        if tt not in new:
            report.changes.append(f"{tt}: removed from the new export")
            continue
        before = _variable_fields(derive, tt, old[tt])
        after = _variable_fields(derive, tt, new[tt])
        retyped = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
        lines = [f"  + {name} ({after[name]})" for name in sorted(after.keys() - before.keys())]
        lines += [f"  - {name} (was {before[name]})" for name in sorted(before.keys() - after.keys())]
        lines += [f"  ~ {name}: {before[name]} -> {after[name]}" for name in retyped]
        if lines:
            report.changes += [f"{tt}:", *lines]
    return report
