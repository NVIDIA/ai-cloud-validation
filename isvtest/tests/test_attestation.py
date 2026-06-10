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

"""Tests for the hardware/firmware attestation validations (SEC22-01 / CNP09-02)."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.attestation import (
    FirmwareAttestationCheck,
    NonceAttestationCheck,
)


def _machine(
    *,
    machine_id: str = "m-001",
    attestation_supported: bool = True,
    nonce_verified: bool = True,
    attestation_signature_valid: bool = True,
    secure_boot_enabled: bool = True,
    boot_measurements_attested: bool = True,
    measured_boot_state: str = "measured",
) -> dict[str, Any]:
    """Build a provider-neutral per-machine attestation record."""
    return {
        "machine_id": machine_id,
        "attestation_supported": attestation_supported,
        "nonce_verified": nonce_verified,
        "attestation_signature_valid": attestation_signature_valid,
        "secure_boot_enabled": secure_boot_enabled,
        "boot_measurements_attested": boot_measurements_attested,
        "measured_boot_state": measured_boot_state,
    }


def _output(
    *,
    success: bool = True,
    machines: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build an attestation step output."""
    if machines is None:
        machines = [_machine()]
    return {
        "success": success,
        "platform": "nico",
        "machines_checked": len(machines),
        "machines": machines,
        "error": error,
    }


# ===========================================================================
# NonceAttestationCheck (SEC22-01)
# ===========================================================================


class TestNonceAttestationCheck:
    """Tests for NonceAttestationCheck validation."""

    def test_fresh_attestation_passes(self) -> None:
        """A host that satisfied the nonce challenge and verified passes."""
        check = NonceAttestationCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error
        sub = next(r for r in check._subtest_results if r["name"] == "nonce_m-001")
        assert sub["passed"] is True

    def test_unverified_nonce_fails(self) -> None:
        """A stale/unsatisfied nonce challenge fails."""
        check = NonceAttestationCheck(config={"step_output": _output(machines=[_machine(nonce_verified=False)])})
        check.run()
        assert check._passed is False
        assert "1/1 machine(s)" in check._error
        sub = next(r for r in check._subtest_results if r["name"].startswith("nonce_"))
        assert "nonce-based attestation not satisfied" in sub["message"]

    def test_invalid_signature_fails(self) -> None:
        """Attestation evidence whose signature does not verify fails."""
        check = NonceAttestationCheck(
            config={"step_output": _output(machines=[_machine(attestation_signature_valid=False)])}
        )
        check.run()
        assert check._passed is False
        sub = next(r for r in check._subtest_results if r["name"].startswith("nonce_"))
        assert "signature did not verify" in sub["message"]

    def test_unsupported_attestation_fails(self) -> None:
        """Hardware that cannot attest fails the requirement."""
        check = NonceAttestationCheck(config={"step_output": _output(machines=[_machine(attestation_supported=False)])})
        check.run()
        assert check._passed is False
        sub = next(r for r in check._subtest_results if r["name"].startswith("nonce_"))
        assert "does not support" in sub["message"]

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = NonceAttestationCheck(config={"step_output": _output(success=False, error="API timeout")})
        check.run()
        assert check._passed is False
        assert "API timeout" in check._error

    def test_skipped_step_skips_validation(self) -> None:
        """A provider-level skip should become a pytest runtime skip."""
        check = NonceAttestationCheck(
            config={"step_output": {"success": True, "skipped": True, "skip_reason": "admin CLI unavailable"}}
        )
        with pytest.raises(pytest.skip.Exception, match="admin CLI unavailable"):
            check.run()

    def test_missing_machines_list(self) -> None:
        """A non-list machines field fails."""
        output = _output()
        output["machines"] = None
        check = NonceAttestationCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "machines" in check._error

    def test_no_machines_fails(self) -> None:
        """An empty machine list fails -- nothing was validated."""
        check = NonceAttestationCheck(config={"step_output": _output(machines=[])})
        check.run()
        assert check._passed is False
        assert "No machines" in check._error

    def test_reports_all_machines_and_summary(self) -> None:
        """One failing host fails the check while the clean host still passes."""
        good = _machine(machine_id="m-good")
        bad = _machine(machine_id="m-bad", nonce_verified=False)
        check = NonceAttestationCheck(config={"step_output": _output(machines=[good, bad])})
        check.run()
        assert check._passed is False
        assert "1/2 machine(s)" in check._error
        names = {r["name"]: r["passed"] for r in check._subtest_results}
        assert names["nonce_m-good"] is True
        assert names["nonce_m-bad"] is False


# ===========================================================================
# FirmwareAttestationCheck (CNP09-02)
# ===========================================================================


class TestFirmwareAttestationCheck:
    """Tests for FirmwareAttestationCheck validation."""

    def test_measured_fleet_passes(self) -> None:
        """A fleet with secure boot and attested boot measurements passes."""
        check = FirmwareAttestationCheck(config={"step_output": _output()})
        check.run()
        assert check._passed is True, check._error
        sub = next(r for r in check._subtest_results if r["name"] == "firmware_m-001")
        assert sub["passed"] is True
        assert "boot measurements attested" in sub["message"]

    def test_secure_boot_disabled_fails(self) -> None:
        """A host without secure boot fails."""
        machine = _machine(secure_boot_enabled=False)
        check = FirmwareAttestationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        sub = next(r for r in check._subtest_results if r["name"].startswith("firmware_"))
        assert "secure boot is not enabled" in sub["message"]

    def test_unattested_measurements_fail_and_surface_state(self) -> None:
        """A host whose boot measurements were not attested fails and surfaces its state."""
        machine = _machine(boot_measurements_attested=False, measured_boot_state="pending_bundle")
        check = FirmwareAttestationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        sub = next(r for r in check._subtest_results if r["name"].startswith("firmware_"))
        assert "not attested against golden values" in sub["message"]
        assert "pending_bundle" in sub["message"]

    def test_unsupported_attestation_fails(self) -> None:
        """Hardware that cannot attest fails the requirement."""
        machine = _machine(attestation_supported=False)
        check = FirmwareAttestationCheck(config={"step_output": _output(machines=[machine])})
        check.run()
        assert check._passed is False
        sub = next(r for r in check._subtest_results if r["name"].startswith("firmware_"))
        assert "does not support" in sub["message"]

    def test_step_failure(self) -> None:
        """A failed step fails the check with its error detail."""
        check = FirmwareAttestationCheck(config={"step_output": _output(success=False, error="API down")})
        check.run()
        assert check._passed is False
        assert "API down" in check._error
