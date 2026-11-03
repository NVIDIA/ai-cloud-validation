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

"""`own-object`: a planned resource creates the object that it owns.

A resource whose create is the same operation as its update, and that has no
delete, sets a value on an object that it does not create. Its destroy cannot
take the value back. A policy set is the common case: GCP's `setIamPolicy`
replaces the whole policy of an object that already exists, so a test of it
writes the policy of the account it runs in. The kit has no resource shape for
a value set on another object.

A create that is also the update, with a delete, is a PUT that creates or
replaces: the resource owns its object, and the delete removes it.
"""

from __future__ import annotations

from collections.abc import Iterable

import operation_refs
from api_spec import Spec


def failures(planned: Iterable[tuple[str, dict[str, str]]], spec: Spec) -> list[str]:
    """One failure for each planned resource that sets a value on an object it
    does not create. `planned` holds each resource's place in the plan and its
    `operations`."""
    found = []
    for where, operations in planned:
        create, update = operations.get("create"), operations.get("update")
        if not create or not update or operations.get("delete"):
            continue
        if operation_refs.identity(create, spec) != operation_refs.identity(update, spec):
            continue
        found.append(
            f"{where}: {create} is both its create and its update, and it has no delete. "
            f"It sets a value on an object that it does not create, and a destroy cannot "
            f"take the value back. Take the resource out of the plan, list {create} under "
            f"module_plan.unplanned_operations with a reason, and state in the layer's gap "
            f"what the requirement loses"
        )
    return found
