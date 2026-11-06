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
"""A committed report is the render of its record, and nothing else.

A reader takes the report as the kit's render of the record. A report written
or edited by hand can state what the record does not, so a report that the
render does not reproduce is refused.
"""

from __future__ import annotations

GATE = "report-is-the-render"


def differences(rendered: str, report: str, name: str) -> list[str]:
    """One finding at the first line where the report leaves the render, or none."""
    want, have = rendered.splitlines(), report.splitlines()
    if want == have:
        return []
    line = next(
        (i for i, (w, h) in enumerate(zip(want, have, strict=False)) if w != h),
        min(len(want), len(have)),
    )
    return [
        f"{name} line {line + 1} is not the render of the record: the render has "
        f"{_at(want, line)}, the report has {_at(have, line)}. Do not edit the report; "
        "render it with `report` from the record, and change the record when the report must say more"
    ]


def _at(lines: list[str], index: int) -> str:
    return repr(lines[index]) if index < len(lines) else "no line"
