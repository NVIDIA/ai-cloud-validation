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
"""Which read proves a destroy: this resource's own, or another one's.

Some resources show a destroy only on another resource. A key that cannot be
deleted outright has its versions destroyed, and the key's own read has no
field that says so. `verification.destroy.via` names that other read, and the
identifier it takes, built from the destroyed resource's own.
"""

from __future__ import annotations

from typing import Any

#: Where the destroyed resource's identifier goes in `via.id`.
OWN_ID = "{id}"
VIA = "via"


def destroy_read(case: dict[str, Any]) -> tuple[str, str]:
    """The operation the destroy check reads, and the identifier it reads, as a
    template over the destroyed resource's identifier."""
    destroy = (case.get("verification") or {}).get("destroy") or {}
    via = destroy.get(VIA) or {}
    if via:
        return str(via.get("operation") or ""), str(via.get("id") or "")
    return str(destroy.get("operation") or ""), OWN_ID


def refusals(case: dict[str, Any]) -> list[str]:
    """Each way `verification.destroy.via` cannot be run, empty when it can."""
    destroy = (case.get("verification") or {}).get("destroy") or {}
    via = destroy.get(VIA)
    if via is None:
        return []
    if not isinstance(via, dict):
        return [f"verification.destroy.{VIA} is not a mapping"]
    found = []
    if not str(via.get("operation") or "").startswith("op:"):
        found.append(f"verification.destroy.{VIA}.operation names no `op:` operation")
    if OWN_ID not in str(via.get("id") or ""):
        # A fixed identifier reads the same resource on every run, whatever
        # this run destroyed.
        found.append(f"verification.destroy.{VIA}.id does not hold {OWN_ID}, the destroyed resource's identifier")
    if not str(via.get("why") or "").strip():
        found.append(
            f"verification.destroy.{VIA} gives no why; a reviewer cannot tell it from a read of the wrong resource"
        )
    return found
