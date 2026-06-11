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

"""Tests for the tenant-transition sanitization validations (SEC21-02/04/05/06)."""

from __future__ import annotations

from typing import Any

from isvtest.validations.sanitization import (
    DiskSanitizationCheck,
    FirmwareResetCheck,
    GpuMemorySanitizationCheck,
    MemorySanitizationCheck,
)


def _machine(
    *,
    machine_id: str = "m-001",
    status: str = "available",
    available: bool = True,
    in_use: bool = False,
    has_gpu: bool = True,
    served_tenant: bool = True,
    sanitized: bool = True,
    stale_tenant_binding: bool = False,
    vendor: str = "Lenovo",
    product_name: str = "ThinkSystem SR670 V2",
    bios_version: str = "U8E122J-1.51",
    transitions: list[str] | None = None,
) -> dict[str, Any]:
    """Build a provider-neutral per-machine sanitization record."""
    return {
        "machine_id": machine_id,
        "status": status,
        "available": available,
        "in_use": in_use,
        "has_gpu": has_gpu,
        "served_tenant": served_tenant,
        "sanitized": sanitized,
        "stale_tenant_binding": stale_tenant_binding,
        "vendor": vendor,
        "product_name": product_name,
        "bios_version": bios_version,
        "transitions": transitions if transitions is not None else ["in_use", "sanitizing", "available"],
    }


def _output(
    *,
    success: bool = True,
    machines: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a sanitization step output."""
    if machines is None:
        machines = [_machine()]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "machines_checked": len(machines),
        "machines": machines,
        "error": error,
    }


# ===========================================================================
# MemorySanitizationCheck (SEC21-04)
# ===========================================================================


class TestMemorySanitizationCheck:
    """Tests for MemorySanitizationCheck validation."""

    def test_sanitized_fleet_passes(self) -> None:
        """A fleet whose released hosts were all sanitized passes."""
        check = MemorySanitizationCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error
        sub = next(r for r in check._subtest_results if r["name"] == "memory_m-001")
        assert sub["passed"] is True

    def test_never_served_tenant_passes(self) -> None:
        """A freshly ingested host with no prior tenancy passes vacuously."""
        machine = _machine(served_tenant=False, transitions=["initializing", "available"])
        check = MemorySanitizationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is True, check._error
        assert "no prior tenancy" in check._subtest_results[0]["message"]

    def test_unsanitized_release_fails(self) -> None:
        """A host returned to the pool without sanitization fails."""
        machine = _machine(sanitized=False, transitions=["in_use", "available"])
        check = MemorySanitizationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        # Summary stays concise (count + offending ids); the full per-machine
        # reason (incl. transitions) is preserved in the subtest message.
        assert "1/1 machine(s)" in check._error
        sub = next(r for r in check._subtest_results if r["name"].startswith("memory_"))
        assert "without sanitization" in sub["message"]
        assert "in_use -> available" in sub["message"]

    def test_stale_tenant_binding_fails(self) -> None:
        """A host offered to new tenants while still bound to a prior tenant fails."""
        machine = _machine(stale_tenant_binding=True)
        check = MemorySanitizationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        assert "1/1 machine(s)" in check._error
        sub = next(r for r in check._subtest_results if r["name"].startswith("memory_"))
        assert "still bound to a prior tenant" in sub["message"]

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = MemorySanitizationCheck(config={"step_output": _output(success=False, error="API timeout")})
        check.run()
        assert check._passed is False
        assert "API timeout" in check._error

    def test_missing_machines_list(self) -> None:
        """A non-list machines field fails."""
        output = _output()
        output["machines"] = None
        check = MemorySanitizationCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "machines" in check._error

    def test_no_machines_fails(self) -> None:
        """An empty machine list fails -- nothing was validated."""
        check = MemorySanitizationCheck(config={"step_output": _output(machines=[])})
        check.run()
        assert check._passed is False
        assert "No machines" in check._error

    def test_reports_all_machines_and_summary(self) -> None:
        """One failing host fails the check while the clean host still passes."""
        good = _machine(machine_id="m-good")
        bad = _machine(machine_id="m-bad", sanitized=False, transitions=["in_use", "available"])
        check = MemorySanitizationCheck(config={"step_output": _output(machines=[good, bad])})
        check.run()
        assert check._passed is False
        assert "1/2 machine(s)" in check._error
        names = {r["name"]: r["passed"] for r in check._subtest_results}
        assert names["memory_m-good"] is True
        assert names["memory_m-bad"] is False


# ===========================================================================
# GpuMemorySanitizationCheck (SEC21-05)
# ===========================================================================


class TestGpuMemorySanitizationCheck:
    """Tests for GpuMemorySanitizationCheck validation."""

    def test_only_gpu_hosts_are_scoped(self) -> None:
        """Non-GPU hosts are ignored; a sanitized GPU host passes."""
        gpu = _machine(machine_id="m-gpu", has_gpu=True)
        cpu = _machine(machine_id="m-cpu", has_gpu=False, sanitized=False, transitions=["in_use", "available"])
        check = GpuMemorySanitizationCheck(config={"step_output": _output(machines=[gpu, cpu])})
        check.run()
        # The CPU host's violation must not count -- it is out of GPU scope.
        assert check._passed is True, check._error
        names = {r["name"] for r in check._subtest_results}
        assert "gpu_memory_m-gpu" in names
        assert "gpu_memory_m-cpu" not in names

    def test_no_gpu_hosts_fails(self) -> None:
        """A fleet with no GPU-equipped host fails (nothing to validate)."""
        cpu = _machine(machine_id="m-cpu", has_gpu=False)
        check = GpuMemorySanitizationCheck(config={"step_output": _output(machines=[cpu])})
        check.run()
        assert check._passed is False
        assert "No GPU-equipped machines" in check._error

    def test_gpu_violation_fails(self) -> None:
        """An unsanitized GPU host fails."""
        gpu = _machine(machine_id="m-gpu", has_gpu=True, sanitized=False, transitions=["in_use", "available"])
        check = GpuMemorySanitizationCheck(config={"step_output": _output(machines=[gpu])})
        check.run()
        assert check._passed is False
        assert "GPU memory sanitization failed" in check._error


# ===========================================================================
# FirmwareResetCheck (SEC21-06 / SEC22)
# ===========================================================================


class TestFirmwareResetCheck:
    """Tests for FirmwareResetCheck validation."""

    def test_sanitized_fleet_passes_and_reports_firmware(self) -> None:
        """A sanitized fleet passes and emits a report-only firmware identity subtest."""
        check = FirmwareResetCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error
        identity = next(r for r in check._subtest_results if r["name"] == "firmware_m-001_identity")
        assert identity["passed"] is True
        assert "BIOS U8E122J-1.51" in identity["message"]

    def test_unsanitized_release_fails(self) -> None:
        """A host returned without TPM/BIOS reset fails the gate."""
        machine = _machine(sanitized=False, transitions=["in_use", "available"])
        check = FirmwareResetCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        assert "Firmware reset failed" in check._error

    def test_missing_bios_version_reports_unknown(self) -> None:
        """A missing BIOS version is reported as unknown, not a failure."""
        machine = _machine(bios_version="")
        check = FirmwareResetCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is True, check._error
        identity = next(r for r in check._subtest_results if r["name"] == "firmware_m-001_identity")
        assert "BIOS unknown" in identity["message"]

    def test_step_failure_skips_firmware_subtests(self) -> None:
        """A failed step fails the check and emits no firmware identity subtests."""
        check = FirmwareResetCheck(config={"step_output": _output(success=False, error="API down")})
        check.run()
        assert check._passed is False
        assert not any(r["name"].endswith("_identity") for r in check._subtest_results)


# ===========================================================================
# DiskSanitizationCheck (SEC21-02)
# ===========================================================================


class TestDiskSanitizationCheck:
    """Tests for DiskSanitizationCheck validation (SEC21-02)."""

    def test_sanitized_fleet_passes(self) -> None:
        """A fleet whose released hosts all passed the sanitizing stage passes."""
        check = DiskSanitizationCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error
        sub = next(r for r in check._subtest_results if r["name"] == "disk_m-001")
        assert sub["passed"] is True

    def test_never_served_tenant_passes(self) -> None:
        """A host with no prior tenancy needs no storage wipe and passes vacuously."""
        machine = _machine(served_tenant=False, transitions=["initializing", "available"])
        check = DiskSanitizationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is True, check._error
        assert "no prior tenancy" in check._subtest_results[0]["message"]

    def test_unsanitized_release_fails(self) -> None:
        """A host returned to the pool without the sanitizing stage fails."""
        machine = _machine(sanitized=False, transitions=["in_use", "available"])
        check = DiskSanitizationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        assert "1/1 machine(s)" in check._error
        sub = next(r for r in check._subtest_results if r["name"] == "disk_m-001")
        assert "without sanitization" in sub["message"]

    def test_stale_tenant_binding_fails(self) -> None:
        """A host still bound to a prior tenant fails the storage gate too."""
        machine = _machine(stale_tenant_binding=True)
        check = DiskSanitizationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        sub = next(r for r in check._subtest_results if r["name"] == "disk_m-001")
        assert "still bound to a prior tenant" in sub["message"]

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = DiskSanitizationCheck(config={"step_output": _output(success=False, error="API timeout")})
        check.run()
        assert check._passed is False
        assert "API timeout" in check._error
