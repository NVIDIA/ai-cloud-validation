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

"""Pure helpers for removing exactly-owned members from IAM policy bindings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.iam.v1 import policy_pb2


def ensure_unconditional_binding_member(policy: Any, role: str, member: str) -> bool:
    """Ensure one member is present in an unconditional role binding.

    Returns ``True`` only when the policy changed. Conditioned bindings for the
    same role are preserved and do not satisfy an unconditional grant.
    """
    for binding in policy.bindings:
        condition = getattr(binding, "condition", None)
        conditioned = bool(
            getattr(condition, "title", "")
            or getattr(condition, "description", "")
            or getattr(condition, "expression", "")
        )
        if binding.role != role or conditioned:
            continue
        if member in binding.members:
            return False
        binding.members.append(member)
        return True
    policy.bindings.append(policy_pb2.Binding(role=role, members=[member]))
    return True


def service_account_member_email(member: str) -> str:
    """Return an SA email from live or deleted IAM member syntax, else empty.

    Google rewrites a deleted principal as
    ``deleted:serviceAccount:<email>?uid=<id>``. Cleanup must still recognize
    the exact email without broadening ownership to another principal type.
    """
    normalized = member.removeprefix("deleted:")
    if not normalized.startswith("serviceAccount:"):
        return ""
    return normalized.removeprefix("serviceAccount:").split("?", 1)[0]


def remove_binding_members(
    policy: Any,
    *,
    binding_matches: Callable[[Any], bool],
    member_matches: Callable[[str], bool],
) -> int:
    """Remove matching members from matching bindings while preserving all others.

    Empty bindings are removed. The policy object is mutated in place so its
    original etag remains attached for an etag-aware write by the caller.
    """
    retained: list[policy_pb2.Binding] = []
    removed = 0
    for binding in policy.bindings:
        clone_fields: dict[str, Any] = {
            "role": binding.role,
            "members": list(binding.members),
        }
        condition = getattr(binding, "condition", None)
        if condition:
            clone_fields["condition"] = condition
        clone = policy_pb2.Binding(**clone_fields)
        if binding_matches(binding):
            remaining = [member for member in binding.members if not member_matches(member)]
            removed += len(binding.members) - len(remaining)
            clone.members[:] = remaining
        if clone.members:
            retained.append(clone)

    if removed:
        del policy.bindings[:]
        policy.bindings.extend(retained)
    return removed
