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

"""Tests for hardware ingestion and DPU health validations."""

from __future__ import annotations

from typing import Any

from isvtest.validations.hardware import (
    DpuHealthCheck,
    DpuNetworkCheck,
    HardwareIngestionCheck,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ingestion_output(
    *,
    success: bool = True,
    expected_count: int = 2,
    ingested_count: int = 2,
    matched_count: int = 2,
    missing: list[dict[str, Any]] | None = None,
    extra: list[dict[str, Any]] | None = None,
    machines: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a minimal hardware ingestion step output."""
    if machines is None:
        machines = [
            {
                "chassis_serial": "SN-001",
                "expected_machine_id": "em-001",
                "machine_id": "m-001",
                "status": "Ready",
                "health": "healthy",
                "gpu_count": 4,
                "dpu_count": 2,
                "capabilities": ["GPU", "DPU", "InfiniBand"],
            },
            {
                "chassis_serial": "SN-002",
                "expected_machine_id": "em-002",
                "machine_id": "m-002",
                "status": "Ready",
                "health": "healthy",
                "gpu_count": 4,
                "dpu_count": 2,
                "capabilities": ["GPU", "DPU", "InfiniBand"],
            },
        ]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "expected_count": expected_count,
        "ingested_count": ingested_count,
        "matched_count": matched_count,
        "missing": missing or [],
        "extra": extra or [],
        "machines": machines,
        "error": error,
    }


def _dpu_health_output(
    *,
    success: bool = True,
    machines: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a minimal DPU health check step output."""
    if machines is None:
        machines = [
            {
                "machine_id": "m-001",
                "chassis_serial": "SN-001",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "healthy",
                "health_successes": [
                    "DpuDiskUtilizationCheck",
                    "BgpDaemonEnabled",
                    "FanSpeed",
                ],
                "health_alerts": [],
                "dpu_agent_heartbeat": True,
            },
        ]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "machines_checked": len(machines),
        "machines": machines,
        "error": error,
    }


def _dpu_network_output(
    *,
    success: bool = True,
    interfaces: list[dict[str, Any]] | None = None,
    bgp_enabled: bool = True,
    dpu_extension_deployments: list[dict[str, Any]] | None = None,
    error: str = "",
    include_bgp: bool = True,
) -> dict[str, Any]:
    """Build a minimal DPU network check step output."""
    if interfaces is None:
        interfaces = [
            {"name": "eth0", "status": "Ready", "type": "ethernet"},
            {"name": "ib0", "status": "Ready", "type": "infiniband"},
        ]
    if dpu_extension_deployments is None:
        dpu_extension_deployments = [
            {"name": "monitoring", "status": "Running", "version": "v1"},
        ]
    result: dict[str, Any] = {
        "success": success,
        "platform": "nico",
        "instance_id": "inst-001",
        "machine_id": "m-001",
        "interfaces": interfaces,
        "dpu_extension_deployments": dpu_extension_deployments,
        "error": error,
    }
    if include_bgp:
        result["bgp_enabled"] = bgp_enabled
    return result


# ===========================================================================
# HardwareIngestionCheck tests
# ===========================================================================


class TestHardwareIngestionCheck:
    """Tests for HardwareIngestionCheck validation."""

    def test_all_machines_ingested_and_healthy(self) -> None:
        """All expected machines are present, Ready, and healthy."""
        check = HardwareIngestionCheck(config={"step_output": _ingestion_output()})
        check.run()
        assert check._passed is True
        assert "2 expected machines ingested" in check._output
        # Each OK machine emits passing status + health subtests (parity with DpuHealthCheck)
        status_subtests = [r for r in check._subtest_results if r.get("name", "").startswith("machine_status_")]
        health_subtests = [r for r in check._subtest_results if r.get("name", "").startswith("machine_health_")]
        assert len(status_subtests) == 2
        assert len(health_subtests) == 2
        assert all(r["passed"] for r in status_subtests + health_subtests)

    def test_step_failure(self) -> None:
        """Step itself failed -- validation should fail with error detail."""
        check = HardwareIngestionCheck(
            config={"step_output": _ingestion_output(success=False, error="API connection timeout")}
        )
        check.run()
        assert check._passed is False
        assert "step failed" in check._error
        assert "API connection timeout" in check._error

    def test_missing_machines(self) -> None:
        """Some expected machines were not ingested."""
        output = _ingestion_output(
            matched_count=1,
            missing=[{"chassis_serial": "SN-002", "expected_machine_id": "em-002"}],
            machines=[
                {
                    "chassis_serial": "SN-001",
                    "expected_machine_id": "em-001",
                    "machine_id": "m-001",
                    "status": "Ready",
                    "health": "healthy",
                    "gpu_count": 4,
                    "dpu_count": 2,
                    "capabilities": ["GPU", "DPU"],
                },
            ],
        )
        check = HardwareIngestionCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "1 missing" in check._error

    def test_machine_bad_status(self) -> None:
        """Machine ingested but in Error status."""
        machines = [
            {
                "chassis_serial": "SN-001",
                "expected_machine_id": "em-001",
                "machine_id": "m-001",
                "status": "Error",
                "health": "unhealthy",
                "gpu_count": 4,
                "dpu_count": 2,
                "capabilities": ["GPU", "DPU"],
            },
        ]
        output = _ingestion_output(expected_count=1, ingested_count=1, matched_count=1, machines=machines)
        check = HardwareIngestionCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "bad status" in check._error
        # Failure summary should identify the offending machine (id + serial)
        assert "m-001" in check._error
        assert "SN-001" in check._error

    def test_machine_unhealthy(self) -> None:
        """Machine ingested and Ready but unhealthy."""
        machines = [
            {
                "chassis_serial": "SN-001",
                "expected_machine_id": "em-001",
                "machine_id": "m-001",
                "status": "Ready",
                "health": "unhealthy",
                "gpu_count": 4,
                "dpu_count": 2,
                "capabilities": ["GPU", "DPU"],
            },
        ]
        output = _ingestion_output(expected_count=1, ingested_count=1, matched_count=1, machines=machines)
        check = HardwareIngestionCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "unhealthy" in check._error

    def test_skip_health_check(self) -> None:
        """require_healthy=False should skip health validation."""
        machines = [
            {
                "chassis_serial": "SN-001",
                "expected_machine_id": "em-001",
                "machine_id": "m-001",
                "status": "Ready",
                "health": "unhealthy",
                "gpu_count": 4,
                "dpu_count": 2,
                "capabilities": ["GPU", "DPU"],
            },
        ]
        output = _ingestion_output(expected_count=1, ingested_count=1, matched_count=1, machines=machines)
        check = HardwareIngestionCheck(config={"step_output": output, "require_healthy": False})
        check.run()
        assert check._passed is True

    def test_below_min_machines(self) -> None:
        """Fewer expected machines than min_machines threshold."""
        output = _ingestion_output(expected_count=0, machines=[])
        check = HardwareIngestionCheck(config={"step_output": output, "min_machines": 1})
        check.run()
        assert check._passed is False
        assert "at least 1" in check._error

    def test_extra_machines_are_informational(self) -> None:
        """Extra machines (ingested but not expected) reported as info, not failure."""
        output = _ingestion_output(
            extra=[{"chassis_serial": "SN-EXTRA", "machine_id": "m-extra"}],
        )
        check = HardwareIngestionCheck(config={"step_output": output})
        check.run()
        # Extra machines are informational -- overall check should pass
        assert check._passed is True
        # Subtest should be passing (informational)
        extra_subtests = [r for r in check._subtest_results if "extra" in r.get("name", "")]
        assert len(extra_subtests) == 1
        assert extra_subtests[0]["passed"] is True
        assert "Info" in extra_subtests[0]["message"]

    def test_custom_expected_status(self) -> None:
        """Custom expected_status list rejects InUse machines."""
        machines = [
            {
                "chassis_serial": "SN-001",
                "expected_machine_id": "em-001",
                "machine_id": "m-001",
                "status": "InUse",
                "health": "healthy",
                "gpu_count": 4,
                "dpu_count": 2,
                "capabilities": ["GPU", "DPU"],
            },
        ]
        output = _ingestion_output(expected_count=1, ingested_count=1, matched_count=1, machines=machines)
        check = HardwareIngestionCheck(config={"step_output": output, "expected_status": ["Ready"]})
        check.run()
        assert check._passed is False
        assert "bad status" in check._error

    def test_empty_step_output(self) -> None:
        """Empty step_output (missing success key) should fail."""
        check = HardwareIngestionCheck(config={"step_output": {}})
        check.run()
        assert check._passed is False
        assert "step failed" in check._error

    def test_no_step_output_key(self) -> None:
        """Config with no step_output key at all should fail."""
        check = HardwareIngestionCheck(config={})
        check.run()
        assert check._passed is False


# ===========================================================================
# DpuHealthCheck tests
# ===========================================================================


class TestDpuHealthCheck:
    """Tests for DpuHealthCheck validation."""

    def test_healthy_dpus(self) -> None:
        """All DPUs healthy with active heartbeat."""
        check = DpuHealthCheck(config={"step_output": _dpu_health_output()})
        check.run()
        assert check._passed is True
        assert "healthy DPUs" in check._output

    def test_step_failure(self) -> None:
        """Step failed -- validation should fail with error detail."""
        check = DpuHealthCheck(config={"step_output": _dpu_health_output(success=False, error="Cannot reach API")})
        check.run()
        assert check._passed is False
        assert "Cannot reach API" in check._error

    def test_no_machines(self) -> None:
        """No machines in output."""
        check = DpuHealthCheck(config={"step_output": _dpu_health_output(machines=[])})
        check.run()
        assert check._passed is False
        assert "No machines" in check._error

    def test_no_dpus_detected(self) -> None:
        """Machine has 0 DPUs -- still checks alerts."""
        machines = [
            {
                "machine_id": "m-001",
                "chassis_serial": "SN-001",
                "status": "Ready",
                "dpu_count": 0,
                "dpu_capability": None,
                "health_summary": "healthy",
                "health_successes": [],
                "health_alerts": [],
                "dpu_agent_heartbeat": False,
            },
        ]
        check = DpuHealthCheck(config={"step_output": _dpu_health_output(machines=machines)})
        check.run()
        assert check._passed is False
        assert "m-001" in check._error

    def test_wrong_dpu_count(self) -> None:
        """DPU count doesn't match expected -- still checks heartbeat and alerts."""
        check = DpuHealthCheck(
            config={
                "step_output": _dpu_health_output(),
                "expected_dpu_count": 4,
            }
        )
        check.run()
        assert check._passed is False
        # Should have count subtest AND heartbeat subtest (no longer skipped via continue)
        subtest_names = [r.get("name", "") for r in check._subtest_results]
        assert any("count" in n for n in subtest_names)
        assert any("heartbeat" in n or "health" in n for n in subtest_names)

    def test_missing_heartbeat(self) -> None:
        """DPU agent heartbeat is missing."""
        machines = [
            {
                "machine_id": "m-001",
                "chassis_serial": "SN-001",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "healthy",
                "health_successes": ["DpuDiskUtilizationCheck"],
                "health_alerts": [],
                "dpu_agent_heartbeat": False,
            },
        ]
        check = DpuHealthCheck(config={"step_output": _dpu_health_output(machines=machines)})
        check.run()
        assert check._passed is False
        assert any("heartbeat" in r.get("message", "").lower() for r in check._subtest_results)

    def test_dpu_health_alerts(self) -> None:
        """DPU has health alerts."""
        machines = [
            {
                "machine_id": "m-001",
                "chassis_serial": "SN-001",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "unhealthy",
                "health_successes": [],
                "health_alerts": [
                    {
                        "id": "HeartbeatTimeout",
                        "target": "nico-dpu-agent",
                        "message": "No heartbeat for 300s",
                    },
                ],
                "dpu_agent_heartbeat": False,
            },
        ]
        check = DpuHealthCheck(config={"step_output": _dpu_health_output(machines=machines)})
        check.run()
        assert check._passed is False

    def test_heartbeat_not_required(self) -> None:
        """require_heartbeat=False skips heartbeat check."""
        machines = [
            {
                "machine_id": "m-001",
                "chassis_serial": "SN-001",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "healthy",
                "health_successes": ["DpuDiskUtilizationCheck"],
                "health_alerts": [],
                "dpu_agent_heartbeat": False,
            },
        ]
        check = DpuHealthCheck(
            config={
                "step_output": _dpu_health_output(machines=machines),
                "require_heartbeat": False,
            }
        )
        check.run()
        assert check._passed is True

    def test_multi_machine_mixed_health(self) -> None:
        """Multiple machines -- some healthy, some not."""
        machines = [
            {
                "machine_id": "m-001",
                "chassis_serial": "SN-001",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "healthy",
                "health_successes": ["DpuDiskUtilizationCheck"],
                "health_alerts": [],
                "dpu_agent_heartbeat": True,
            },
            {
                "machine_id": "m-002",
                "chassis_serial": "SN-002",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "unhealthy",
                "health_successes": [],
                "health_alerts": [
                    {"id": "HeartbeatTimeout", "target": "nico-dpu-agent", "message": "No heartbeat"},
                ],
                "dpu_agent_heartbeat": False,
            },
            {
                "machine_id": "m-003",
                "chassis_serial": "SN-003",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "healthy",
                "health_successes": ["DpuDiskUtilizationCheck"],
                "health_alerts": [],
                "dpu_agent_heartbeat": True,
            },
        ]
        check = DpuHealthCheck(config={"step_output": _dpu_health_output(machines=machines)})
        check.run()
        assert check._passed is False
        assert "1/3" in check._error
        assert "m-002" in check._error
        assert "SN-002" in check._error  # offending machine's serial is surfaced
        assert "m-001" not in check._error
        assert "m-003" not in check._error

    def test_unhealthy_summary_without_dpu_alerts(self) -> None:
        """Machine unhealthy from non-DPU alerts -- still flagged via health_summary."""
        machines = [
            {
                "machine_id": "m-001",
                "chassis_serial": "SN-001",
                "status": "Ready",
                "dpu_count": 2,
                "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
                "health_summary": "unhealthy",
                "health_successes": [],
                "health_alerts": [],  # No DPU-specific alerts, but machine is unhealthy
                "dpu_agent_heartbeat": True,
            },
        ]
        check = DpuHealthCheck(config={"step_output": _dpu_health_output(machines=machines)})
        check.run()
        assert check._passed is False

    def test_empty_step_output(self) -> None:
        """Empty step_output should fail."""
        check = DpuHealthCheck(config={"step_output": {}})
        check.run()
        assert check._passed is False


# ===========================================================================
# DpuNetworkCheck tests
# ===========================================================================


class TestDpuNetworkCheck:
    """Tests for DpuNetworkCheck validation."""

    def test_all_healthy(self) -> None:
        """All interfaces Ready, BGP enabled, extensions running."""
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output()})
        check.run()
        assert check._passed is True

    def test_step_failure(self) -> None:
        """Step failed."""
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(success=False, error="EVPN not configured")})
        check.run()
        assert check._passed is False
        assert "EVPN not configured" in check._error

    def test_no_interfaces(self) -> None:
        """No interfaces found."""
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(interfaces=[])})
        check.run()
        assert check._passed is False
        assert "No network interfaces" in check._error

    def test_interface_not_ready(self) -> None:
        """Some interfaces in Error state."""
        interfaces = [
            {"name": "eth0", "status": "Ready", "type": "ethernet"},
            {"name": "ib0", "status": "Error", "type": "infiniband"},
        ]
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(interfaces=interfaces)})
        check.run()
        assert check._passed is False

    def test_bgp_not_enabled(self) -> None:
        """BGP daemon not running when required."""
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(bgp_enabled=False)})
        check.run()
        assert check._passed is False

    def test_bgp_not_required(self) -> None:
        """BGP not required -- should pass without it."""
        check = DpuNetworkCheck(
            config={
                "step_output": _dpu_network_output(bgp_enabled=False),
                "require_bgp": False,
            }
        )
        check.run()
        assert check._passed is True

    def test_bgp_field_missing(self) -> None:
        """Missing bgp_enabled field should fail (malformed payload)."""
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(include_bgp=False)})
        check.run()
        assert check._passed is False
        assert any("not reported" in r.get("message", "") for r in check._subtest_results)

    def test_failed_extension_deployment(self) -> None:
        """DPU extension service in Error state."""
        deployments = [
            {"name": "monitoring", "status": "Error", "version": "v1"},
        ]
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(dpu_extension_deployments=deployments)})
        check.run()
        assert check._passed is False

    def test_pending_extension_acceptable(self) -> None:
        """Pending DPU extension should be acceptable (not flagged as failure)."""
        deployments = [
            {"name": "monitoring", "status": "Pending", "version": "v1"},
        ]
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(dpu_extension_deployments=deployments)})
        check.run()
        assert check._passed is True

    def test_mixed_extension_statuses(self) -> None:
        """Mix of Running and Error extensions -- only Error reported."""
        deployments = [
            {"name": "monitoring", "status": "Running", "version": "v1"},
            {"name": "logging", "status": "Error", "version": "v2"},
            {"name": "metrics", "status": "Running", "version": "v1"},
        ]
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(dpu_extension_deployments=deployments)})
        check.run()
        assert check._passed is False
        # Should mention only the failed one
        assert any("logging" in r.get("message", "") for r in check._subtest_results)

    def test_extension_missing_status_fails(self) -> None:
        """Extension with no status field should fail (not silently pass)."""
        deployments = [
            {"name": "monitoring", "version": "v1"},  # No status field
        ]
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(dpu_extension_deployments=deployments)})
        check.run()
        assert check._passed is False

    def test_no_extensions(self) -> None:
        """No DPU extensions deployed -- should still pass."""
        check = DpuNetworkCheck(config={"step_output": _dpu_network_output(dpu_extension_deployments=[])})
        check.run()
        assert check._passed is True

    def test_empty_step_output(self) -> None:
        """Empty step_output should fail."""
        check = DpuNetworkCheck(config={"step_output": {}})
        check.run()
        assert check._passed is False
