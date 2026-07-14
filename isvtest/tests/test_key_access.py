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

"""Tests for the specified-key access validation (AUTH-XX-03)."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.key_access import SpecifiedKeyAccessCheck


def _serial_target(
    *,
    key_access_enabled: bool | None = True,
    reachable: bool = True,
    name: str = "sjc-1 serial console (SOL)",
    detail: str = "ok",
) -> dict[str, Any]:
    """Build a serial-console (SOL) access target."""
    return {
        "type": "serial_console",
        "name": name,
        "key_access_enabled": key_access_enabled,
        "reachable": reachable,
        "detail": detail,
    }


def _network_target() -> dict[str, Any]:
    """Build the (unverified) network-device access target."""
    return {
        "type": "network_device",
        "name": "Network devices",
        "key_access_enabled": None,
        "reachable": False,
        "detail": "provider-managed; not verifiable from tenant API",
    }


def _output(
    *,
    success: bool = True,
    specified_keys: int = 1,
    targets: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a specified-key access step output."""
    if targets is None:
        targets = [_serial_target(), _network_target()]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "specified_keys": specified_keys,
        "access_targets": targets,
        "error": error,
    }


class TestSpecifiedKeyAccessCheck:
    """Tests for SpecifiedKeyAccessCheck (AUTH-XX-03)."""

    def test_serial_console_accessible_passes(self) -> None:
        """A key synced to a SOL-enabled, SSH-key-auth site passes."""
        check = SpecifiedKeyAccessCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error
        assert "can access" in check._output
        # The network-device target is reported as a skipped subtest, not a failure.
        net = next(r for r in check._subtest_results if r["name"] == "target_network_device")
        assert net["skipped"] is True
        sol = next(r for r in check._subtest_results if r["name"] == "target_serial_console")
        assert sol["passed"] is True

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(success=False, error="API timeout")})
        check.run()
        assert check._passed is False
        assert "API timeout" in check._error

    def test_structured_skip(self) -> None:
        """A structured skip skips the validation."""
        check = SpecifiedKeyAccessCheck(
            config={"step_output": {"success": True, "skipped": True, "skip_reason": "SOL not configured"}}
        )
        with pytest.raises(pytest.skip.Exception, match="SOL not configured"):
            check.run()

    def test_no_specified_keys_skips(self) -> None:
        """Without any specified key there is nothing to evidence access with."""
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(specified_keys=0)})
        with pytest.raises(pytest.skip.Exception, match="No tenant-specified key"):
            check.run()

    def test_key_access_disabled_fails(self) -> None:
        """A component whose key access is explicitly disabled fails."""
        targets = [_serial_target(key_access_enabled=False, reachable=False, detail="SSH-key access disabled")]
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(targets=targets)})
        check.run()
        assert check._passed is False
        assert "key access disabled" in check._error

    def test_enabled_but_unreachable_fails(self) -> None:
        """Key access enabled but the key has not propagated is a broken path."""
        targets = [_serial_target(key_access_enabled=True, reachable=False, detail="key not synced")]
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(targets=targets)})
        check.run()
        assert check._passed is False
        assert "not reachable" in check._error

    def test_string_reachable_false_does_not_count_as_accessible(self) -> None:
        """Malformed provider output must not coerce the string 'false' to reachable."""
        targets = [
            {
                "type": "serial_console",
                "name": "sjc-1 serial console (SOL)",
                "key_access_enabled": True,
                "reachable": "false",
                "detail": "bad type",
            }
        ]
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(targets=targets)})
        check.run()
        assert check._passed is False
        assert "not reachable" in check._error

    def test_only_unverified_targets_skips(self) -> None:
        """When the only targets are unverifiable, the check skips."""
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(targets=[_network_target()])})
        with pytest.raises(pytest.skip.Exception, match="could not verify"):
            check.run()

    def test_missing_targets_list_fails(self) -> None:
        """A non-list access_targets field fails."""
        output = _output()
        output["access_targets"] = None
        check = SpecifiedKeyAccessCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "access_targets" in check._error

    def test_missing_specified_keys_fails(self) -> None:
        """A missing/non-int specified_keys field fails."""
        output = _output()
        output["specified_keys"] = "1"
        check = SpecifiedKeyAccessCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "specified_keys" in check._error

    def test_min_accessible_targets_enforced(self) -> None:
        """Requiring more accessible targets than available skips (network unverified)."""
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(), "min_accessible_targets": 2})
        with pytest.raises(pytest.skip.Exception, match="could not verify"):
            check.run()

    def test_invalid_min_accessible_targets_fails(self) -> None:
        """A non-integer min_accessible_targets is rejected."""
        check = SpecifiedKeyAccessCheck(config={"step_output": _output(), "min_accessible_targets": "two"})
        check.run()
        assert check._passed is False
        assert "min_accessible_targets" in check._error
