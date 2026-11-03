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
"""`nested-completeness`: a planned resource names its direct nested creations.

`parent-completeness` reads upward from a cited creation; this gate reads
downward from a planned one. A planned resource owes a mention of each
creation one collection level directly below the object it creates -- cited,
planned, offered as a scope alternative, or excused with a reason -- because
a dropped nested creation passes every other gate. A creation two levels down
is owed by the object one level down.

Nesting is read inside the document that defines the planned create. Merged
documents share path prefixes, and another API's collection below the same
path is not inside the planned resource.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

import operation_refs
from api_spec import Operation, Spec


def failures(
    planned: Iterable[tuple[str, str]], named: set[Operation | str], documents: Mapping[Path, Spec]
) -> list[str]:
    """One failure for each direct nested creation of a planned create that
    the artifact names nowhere. `planned` holds each resource's place in the
    plan and its create reference; `named` holds what the artifact names,
    resolved; `documents` holds each supplied specification alone."""
    found = []
    for where, create in planned:
        home = next((d for d in documents.values() if operation_refs.resolve(create, d)), None)
        operation = operation_refs.resolve(create, home) if home else None
        if operation is None:
            continue
        for nested in operation_refs.nested_creations(operation, home):
            if nested in named:
                continue
            found.append(
                f"{where}: creates {operation_refs.describe(operation)}, and the specification "
                f"creates {operation_refs.describe(nested)} one level directly inside it, which "
                f"this analysis never names. Cite it, plan it, or list it under "
                f"module_plan.unplanned_operations with a reason"
            )
    return found
