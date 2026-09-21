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

"""Composite checks: one named validation assembled from generic ones.

Several generic checks often add up to a single thing an operator cares about -
"the IAM user was created with usable credentials" is a step that succeeded
*and* an output carrying the expected fields. Wiring those as separate checks
spends two catalog names on one idea and leaves the second one holding a
``test_id: "N/A"``.

A composite gives that idea its own name, description, and ``test_id``, and
names the generic checks that implement it::

    IamUserCreatedCheck:
      test_id: "IAM01-01"
      labels: ["iam"]
      requires: []
      description: "Check the IAM user is created with usable credentials"
      compose:
        - StepSuccessCheck
        - FieldExistsCheck:
            fields: ["username", "access_key_id"]

The composite is one catalog entry and one pytest test; its members run as
subtests, so a failure still says which part broke.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from isvtest.core.validation import BaseValidation, get_validation_class

COMPOSE_KEY = "compose"

# Wiring metadata that describes the composite itself. It is not a check
# parameter, so it is not passed down to members.
_WIRING_KEYS = frozenset({COMPOSE_KEY, "test_id", "labels", "description"})


def is_composite(params: Any) -> bool:
    """Return whether a check's wiring params declare a composite."""
    return isinstance(params, dict) and params.get(COMPOSE_KEY) is not None


def composed_members(value: Any) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(check_name, params)`` for each member of a ``compose`` list.

    Both ``- CheckName`` and ``- CheckName: {params}`` are accepted so a member
    that takes no parameters stays a single line. Items in neither form are
    dropped; :mod:`scripts.validate_suite_wiring` rejects them at authoring time
    and :meth:`CompositeCheck.run` fails on them at runtime, so they cannot
    silently shrink a composite.
    """
    if not isinstance(value, list):
        return []
    members: list[tuple[str, dict[str, Any]]] = []
    for item in value:
        if isinstance(item, str) and item:
            members.append((item, {}))
        elif isinstance(item, dict):
            for name, params in item.items():
                members.append((str(name), params if isinstance(params, dict) else {}))
    return members


class CompositeCheck(BaseValidation):
    """Run the generic checks a composite names, reporting each as a subtest."""

    description: ClassVar[str] = "Composite of generic checks"
    # Machinery rather than a check: it is never discovered, catalogued, or
    # wired by name - a composite's own wiring name resolves to it instead.
    _exclude_from_discovery: ClassVar[bool] = True

    def run(self) -> None:
        """Run every member, retaining skips and failing on invalid or failed members."""
        raw = self.config.get(COMPOSE_KEY)
        members = composed_members(raw)
        if not members:
            self.set_failed(f"'{COMPOSE_KEY}' must be a non-empty list of check names")
            return

        # An out-of-tree config never meets validate_suite_wiring, so a malformed
        # member would otherwise shrink the composite and still report a pass.
        if isinstance(raw, list) and len(members) != len(raw):
            self.set_failed(
                f"'{COMPOSE_KEY}' has {len(raw) - len(members)} malformed member(s); "
                "each must be 'CheckName' or 'CheckName: {params}'"
            )
            return

        shared = {key: value for key, value in self.config.items() if key not in _WIRING_KEYS}
        outputs: list[str] = []
        failures: list[str] = []

        for member_name, member_params in members:
            member_class = get_validation_class(member_name)
            if member_class is None:
                failures.append(f"{member_name}: unknown validation class")
                self.report_subtest(member_name, False, "unknown validation class")
                continue

            member = member_class(runner=self.runner, config={**shared, **member_params})
            member.name = member_name
            try:
                result = member.execute()
            except pytest.skip.Exception as exc:
                reason = str(exc)
                self.report_subtest(member_name, False, reason, skipped=True)
                outputs.append(f"{member_name}: skipped - {reason}")
                continue
            message = result["output"] if result["passed"] else result["error"]
            self.report_subtest(member_name, result["passed"], message, duration=result["duration"])
            for nested in result.get("subtests", []):
                self.report_subtest(
                    f"{member_name}/{nested['name']}",
                    bool(nested.get("passed")),
                    str(nested.get("message", "")),
                    skipped=bool(nested.get("skipped")),
                    duration=nested.get("duration"),
                )
            if result["passed"]:
                outputs.append(f"{member_name}: {message}" if message else member_name)
            else:
                failures.append(f"{member_name}: {message}")

        if failures:
            self.set_failed("; ".join(failures))
        else:
            self.set_passed("; ".join(outputs))
