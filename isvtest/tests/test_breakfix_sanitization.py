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

"""Tests for STG02 break/fix skip-sanitization validation."""

from __future__ import annotations

from typing import Any

from isvtest.validations.sanitization import SkipSanitizationBreakfixCheck


def _machine(
    machine_id: str = "m-001",
    *,
    served_tenant: bool = True,
    sanitized: bool = True,
    breakfix_skip_observed: bool = False,
    tenancy_preserved: bool = True,
    stale_tenant_binding: bool = False,
) -> dict[str, Any]:
    """Build a provider-neutral sanitization record with STG02 fields."""
    return {
        "machine_id": machine_id,
        "status": "in_use",
        "available": False,
        "in_use": True,
        "instance_bound": True,
        "has_gpu": False,
        "served_tenant": served_tenant,
        "sanitized": sanitized,
        "breakfix_skip_observed": breakfix_skip_observed,
        "tenancy_preserved": tenancy_preserved,
        "stale_tenant_binding": stale_tenant_binding,
        "transitions": ["in_use", "maintenance", "in_use"],
    }


def _output(*, machines: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a sanitization step output."""
    if machines is None:
        machines = [_machine()]
    return {
        "success": True,
        "platform": "nico",
        "site_id": "site-1",
        "machines_checked": len(machines),
        "machines": machines,
    }


class TestSkipSanitizationBreakfixCheck:
    """Tests for SkipSanitizationBreakfixCheck validation (STG02-01)."""

    def test_valid_breakfix_skip_passes(self) -> None:
        """A tenancy-preserving maintenance skip passes."""
        check = SkipSanitizationBreakfixCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error
        assert "maintenance skip" in check._output

    def test_no_breakfix_history_passes(self) -> None:
        """Sites with no maintenance skips still pass as auditable."""
        machine = _machine(breakfix_skip_observed=False)
        check = SkipSanitizationBreakfixCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is True, check._error
        assert "no tenancy-preserving maintenance skips" in check._output

    def test_unsanitized_tenant_release_fails(self) -> None:
        """Tenant release without sanitizing still fails."""
        machine = _machine(sanitized=False, breakfix_skip_observed=False)
        check = SkipSanitizationBreakfixCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False

    def test_breakfix_without_tenancy_preservation_fails(self) -> None:
        """A maintenance skip that drops tenant binding fails."""
        machine = _machine(breakfix_skip_observed=True, tenancy_preserved=False)
        check = SkipSanitizationBreakfixCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        sub = next(r for r in check._subtest_results if r["name"] == "breakfix_skip_m-001")
        assert "tenancy was not preserved" in sub["message"]
