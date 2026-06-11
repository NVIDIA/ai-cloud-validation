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

"""Tests for instance/VM validations."""

from __future__ import annotations

from typing import Any

from isvtest.validations.instance import (
    SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
    InstanceRebootCheck,
    InstanceSpecifiedKeyCheck,
    SerialConsoleRetentionCheck,
)


def _reboot_output(**overrides: Any) -> dict[str, Any]:
    """Build a minimal passing reboot step_output; overrides replace keys."""
    base: dict[str, Any] = {
        "instance_id": "i-abc123",
        "reboot_initiated": True,
        "state": "running",
        "ssh_ready": True,
        "uptime_seconds": 45.2,
        "reboot_confirmed": True,
    }
    base.update(overrides)
    return base


def _retention_output(**overrides: Any) -> dict[str, Any]:
    """Build a minimal passing serial-console retention step_output."""
    base: dict[str, Any] = {
        "instance_id": "bm-abc123",
        "console_log_queryable": True,
        "retention_days_required": SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
        "retention_days_configured": SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
        "oldest_queryable_log_age_days": SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
        "query_result_count": 1,
        "retention_evidence": "test-log-archive:bm-abc123",
    }
    base.update(overrides)
    return base


class TestInstanceRebootCheck:
    """Tests for InstanceRebootCheck - the check must require an affirmative
    ``reboot_confirmed: True`` rather than treating absence as success."""

    def test_passes_with_affirmative_confirmation(self) -> None:
        """Happy path: reboot_confirmed=True passes."""
        v = InstanceRebootCheck(config={"step_output": _reboot_output()})
        result = v.execute()
        assert result["passed"] is True

    def test_fails_when_reboot_confirmed_absent(self) -> None:
        """Absent key must FAIL (was silently passing)."""
        out = _reboot_output()
        del out["reboot_confirmed"]
        v = InstanceRebootCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "not affirmatively confirmed" in result["error"]

    def test_fails_when_reboot_confirmed_none(self) -> None:
        """Explicit None must FAIL - same semantic as absent."""
        v = InstanceRebootCheck(config={"step_output": _reboot_output(reboot_confirmed=None)})
        result = v.execute()
        assert result["passed"] is False
        assert "not affirmatively confirmed" in result["error"]

    def test_fails_when_reboot_confirmed_false(self) -> None:
        """Existing behavior preserved: explicit False still fails."""
        v = InstanceRebootCheck(config={"step_output": _reboot_output(reboot_confirmed=False)})
        result = v.execute()
        assert result["passed"] is False

    def test_fails_when_reboot_initiated_false(self) -> None:
        """Upstream failure mode: reboot API call never succeeded."""
        v = InstanceRebootCheck(config={"step_output": _reboot_output(reboot_initiated=False)})
        result = v.execute()
        assert result["passed"] is False
        assert "Reboot was not initiated" in result["error"]

    def test_fails_when_state_not_running(self) -> None:
        """Instance must be running after reboot."""
        v = InstanceRebootCheck(config={"step_output": _reboot_output(state="stopped")})
        result = v.execute()
        assert result["passed"] is False
        assert "not running" in result["error"]

    def test_fails_when_ssh_not_ready(self) -> None:
        """SSH connectivity must be restored post-reboot."""
        v = InstanceRebootCheck(config={"step_output": _reboot_output(ssh_ready=False)})
        result = v.execute()
        assert result["passed"] is False
        assert "SSH not ready" in result["error"]

    def test_fails_when_uptime_exceeds_max(self) -> None:
        """Uptime > max_uptime means the instance wasn't really rebooted."""
        v = InstanceRebootCheck(config={"step_output": _reboot_output(uptime_seconds=3600), "max_uptime": 600})
        result = v.execute()
        assert result["passed"] is False
        assert "reboot may not have occurred" in result["error"]


class TestInstanceSpecifiedKeyCheck:
    """Tests for VM launch-with-specified-key validation."""

    def test_passes_when_instance_key_matches_requested_key(self) -> None:
        """Provider output proves the launched instance uses the requested key."""
        v = InstanceSpecifiedKeyCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "requested_key_name": "isv-test-key",
                    "key_name": "isv-test-key",
                }
            }
        )

        result = v.execute()

        assert result["passed"] is True
        assert "isv-test-key" in result["output"]

    def test_passes_with_instance_key_name_alias(self) -> None:
        """Providers may emit instance_key_name when key_name is reserved elsewhere."""
        v = InstanceSpecifiedKeyCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "requested_key_name": "custom-key",
                    "instance_key_name": "custom-key",
                }
            }
        )

        result = v.execute()

        assert result["passed"] is True

    def test_fails_when_requested_key_is_missing(self) -> None:
        """The provider must state which key it requested."""
        v = InstanceSpecifiedKeyCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "key_name": "isv-test-key",
                }
            }
        )

        result = v.execute()

        assert result["passed"] is False
        assert "No 'requested_key_name'" in result["error"]

    def test_fails_when_actual_key_is_missing(self) -> None:
        """The provider must report the key observed on the launched instance."""
        v = InstanceSpecifiedKeyCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "requested_key_name": "isv-test-key",
                }
            }
        )

        result = v.execute()

        assert result["passed"] is False
        assert "No launched instance key name" in result["error"]

    def test_fails_when_actual_key_differs_from_requested_key(self) -> None:
        """A launched instance with the wrong key fails the validation."""
        v = InstanceSpecifiedKeyCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "requested_key_name": "isv-test-key",
                    "key_name": "other-key",
                }
            }
        )

        result = v.execute()

        assert result["passed"] is False
        assert "expected key 'isv-test-key', got 'other-key'" in result["error"]


class TestSerialConsoleRetentionCheck:
    """Tests for one-month serial console retention evidence validation."""

    def test_passes_with_complete_retention_evidence(self) -> None:
        """Happy path: queryable logs satisfy the required retention window."""
        v = SerialConsoleRetentionCheck(
            config={
                "step_output": _retention_output(),
                "retention_days_required": SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "bm-abc123" in result["output"]
        assert f"configured={SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED}d" in result["output"]
        assert "test-log-archive:bm-abc123" in result["output"]

    def test_fails_when_instance_id_is_missing(self) -> None:
        """The provider output must identify the node being checked."""
        out = _retention_output()
        del out["instance_id"]
        v = SerialConsoleRetentionCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "No 'instance_id'" in result["error"]

    def test_fails_when_console_logs_are_not_queryable(self) -> None:
        """A provider must prove historical logs can be queried."""
        v = SerialConsoleRetentionCheck(config={"step_output": _retention_output(console_log_queryable=False)})
        result = v.execute()
        assert result["passed"] is False
        assert "not queryable" in result["error"]

    def test_fails_when_configured_retention_is_below_required(self) -> None:
        """Configured retention must meet the required minimum."""
        v = SerialConsoleRetentionCheck(
            config={
                "step_output": _retention_output(retention_days_configured=14),
                "retention_days_required": SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert f"below required {SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED}" in result["error"]

    def test_fails_when_oldest_queryable_age_is_below_required(self) -> None:
        """The returned evidence must cover the full retention window."""
        v = SerialConsoleRetentionCheck(
            config={
                "step_output": _retention_output(oldest_queryable_log_age_days=7),
                "retention_days_required": SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "Oldest queryable serial console log" in result["error"]
        assert f"below required {SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED}" in result["error"]

    def test_fails_when_query_returns_no_records(self) -> None:
        """A configured retention policy alone is insufficient without query results."""
        v = SerialConsoleRetentionCheck(config={"step_output": _retention_output(query_result_count=0)})
        result = v.execute()
        assert result["passed"] is False
        assert "returned no records" in result["error"]

    def test_fails_when_retention_evidence_is_missing(self) -> None:
        """Passing retention fields still require an evidence source."""
        out = _retention_output()
        del out["retention_evidence"]
        v = SerialConsoleRetentionCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "No 'retention_evidence'" in result["error"]
