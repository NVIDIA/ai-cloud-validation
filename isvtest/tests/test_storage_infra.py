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

"""Tests for storage-infrastructure observability validations (STG03/STG04)."""

from __future__ import annotations

from typing import Any

from isvtest.validations.storage_infra import OobFailureDetectionCheck, StableStorageNodeIpCheck


def _host(
    host_id: str = "m-001",
    *,
    primary_ip_addresses: list[str] | None = None,
    hw_sku_device_type: str = "storage",
) -> dict[str, Any]:
    """Build a provider-neutral stable-IP host record."""
    return {
        "host_id": host_id,
        "hw_sku_device_type": hw_sku_device_type,
        "primary_ip_addresses": primary_ip_addresses if primary_ip_addresses is not None else ["10.0.0.5"],
    }


def _oob_host(
    host_id: str = "m-001",
    *,
    oob_health_present: bool = True,
    bmc_probe_ids: list[str] | None = None,
    failure_categories: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a provider-neutral OOB health host record."""
    probes = bmc_probe_ids if bmc_probe_ids is not None else ["BmcSensor"]
    categories = failure_categories
    if categories is None:
        categories = {
            "device": {"observable": True, "probe_ids": ["BmcSensor"]},
            "network": {"observable": False, "probe_ids": []},
            "memory": {"observable": False, "probe_ids": []},
            "drive": {"observable": False, "probe_ids": []},
        }
    return {
        "host_id": host_id,
        "oob_health_present": oob_health_present,
        "bmc_probe_ids": probes,
        "failure_categories": categories,
    }


def _output(*, hosts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a step output envelope."""
    if hosts is None:
        hosts = [_host()]
    return {
        "success": True,
        "platform": "nico",
        "site_id": "site-1",
        "hosts_checked": len(hosts),
        "hosts": hosts,
    }


class TestStableStorageNodeIpCheck:
    """Tests for StableStorageNodeIpCheck validation (STG03-01)."""

    def test_hosts_with_ips_pass(self) -> None:
        """Every host reporting admin IPs passes."""
        check = StableStorageNodeIpCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error

    def test_missing_ip_fails(self) -> None:
        """A host with no admin IPs fails."""
        host = _host(primary_ip_addresses=[])
        check = StableStorageNodeIpCheck(config={"step_output": _output(hosts=[host])})
        check.run()
        assert check._passed is False
        assert "m-001" in check._error

    def test_storage_only_filter(self) -> None:
        """storage_only scopes validation to storage SKU hosts."""
        hosts = [_host(hw_sku_device_type="cpu"), _host(host_id="m-002", hw_sku_device_type="storage")]
        check = StableStorageNodeIpCheck(config={"step_output": _output(hosts=hosts), "storage_only": True})
        check.run()
        assert check._passed is True, check._error


class TestOobFailureDetectionCheck:
    """Tests for OobFailureDetectionCheck validation (STG04-01)."""

    def test_bmc_coverage_passes(self) -> None:
        """Hosts with BmcSensor and device observability pass."""
        check = OobFailureDetectionCheck(config={"step_output": _output(hosts=[_oob_host()])})
        check.run()
        assert check._passed is True, check._error

    def test_missing_bmc_probe_fails(self) -> None:
        """Missing required BMC probes fails the host."""
        host = _oob_host(bmc_probe_ids=["BgpDaemonEnabled"])
        check = OobFailureDetectionCheck(config={"step_output": _output(hosts=[host])})
        check.run()
        assert check._passed is False
        assert "BmcSensor" in check._error

    def test_missing_oob_report_fails(self) -> None:
        """Absent OOB health report fails when required."""
        host = _oob_host(oob_health_present=False, bmc_probe_ids=[])
        check = OobFailureDetectionCheck(config={"step_output": _output(hosts=[host])})
        check.run()
        assert check._passed is False
