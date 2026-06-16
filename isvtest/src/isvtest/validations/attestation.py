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

"""Hardware/firmware attestation validations (requirements SEC22 / CNP09).

Two provider-agnostic checks that assert a cloud's bare-metal hosts establish a
hardware root of trust. They are modelled on the two attestation subsystems a
GPU cloud control plane (e.g. NVIDIA's NICo / infra-controller) actually runs:

- ``NonceAttestationCheck`` (SEC22-01): each host passes a *fresh* nonce-based
  device attestation. This mirrors NICo's **SPDM device attestation**, where the
  verifier issues a random nonce, the device's root of trust (GPU/CPU/BMC ERoT)
  returns signed evidence over that nonce, and a verifier (NRAS) checks it. The
  host passes when the nonce challenge is satisfied (``nonce_verified`` -- proves
  liveness, not a replay) and the returned evidence signature verifies
  (``attestation_signature_valid``).
- ``FirmwareAttestationCheck`` (CNP09-02): all firmware is cryptographically
  signed and its measurements are attested during boot. This mirrors NICo's
  **Measured Boot**, where firmware/bootloader/kernel measurements are extended
  into TPM PCRs during boot and verified against a golden bundle. The host passes
  when secure boot is enabled (``secure_boot_enabled`` -- firmware signatures
  enforced, PCR 7) and the boot measurements were attested against the expected
  golden values (``boot_measurements_attested``).

Both only inspect the provider-neutral JSON a step script emits (one record per
host), so any provider that maps its attestation surface into the documented
fields can reuse them. A host that does not support/expose attestation
(``attestation_supported`` false) fails -- the requirement is that the hardware
*passes* attestation.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar

import pytest

from isvtest.core.validation import BaseValidation


def _machine_label(machine: dict[str, Any]) -> str:
    """Human-facing identifier for a machine record."""
    return machine.get("machine_id") or "unknown"


class _AttestationCheck(BaseValidation):
    """Shared machinery for the per-machine attestation checks.

    Subclasses implement ``_evaluate(machine) -> (passed, message)`` and set the
    ``subject`` / ``subtest_prefix`` wording. Each keeps its own ``description``
    and ``labels`` so it maps to a single test ID and can be toggled
    independently in a suite. A machine that does not support attestation fails
    uniformly (handled here) before the subclass gate runs.

    ``_evaluate`` is abstract so this base helper stays abstract and is excluded
    from validation discovery (it is not a runnable check on its own).
    """

    timeout: ClassVar[int] = 120
    subject: ClassVar[str] = "Attestation"
    subtest_prefix: ClassVar[str] = "attestation"

    @abstractmethod
    def _evaluate(self, machine: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(passed, message)`` for one machine (attestation supported)."""
        raise NotImplementedError

    def _evaluate_machine(self, machine: dict[str, Any]) -> tuple[bool, str]:
        """Gate on attestation support, then defer to the subclass evaluation."""
        label = _machine_label(machine)
        if not machine.get("attestation_supported"):
            return False, f"{label}: hardware does not support/expose attestation"
        return self._evaluate(machine)

    def run(self) -> None:
        """Validate that every machine passes the subclass attestation gate."""
        step_output = self.config.get("step_output", {})

        if step_output.get("skipped"):
            pytest.skip(step_output.get("skip_reason") or "Attestation validation skipped")

        if not step_output.get("success"):
            self.set_failed(f"Attestation step failed: {step_output.get('error', 'Unknown error')}")
            return

        machines = step_output.get("machines")
        if not isinstance(machines, list):
            self.set_failed("Attestation step output is missing the 'machines' list")
            return

        if not machines:
            self.set_failed("No machines found in step output")
            return

        failed: dict[str, str] = {}
        for machine in machines:
            label = _machine_label(machine)
            passed, message = self._evaluate_machine(machine)
            self.report_subtest(f"{self.subtest_prefix}_{label}", passed=passed, message=message)
            if not passed:
                failed[label] = message

        total = len(machines)
        if failed:
            # Keep the summary concise: name a few offenders and a count. The
            # full per-machine reason is preserved in the subtests.
            sample = ", ".join(list(failed)[:3])
            more = len(failed) - min(len(failed), 3)
            summary = f"{sample} (+{more} more)" if more else sample
            self.set_failed(f"{self.subject} failed for {len(failed)}/{total} machine(s): {summary}")
            return

        self.set_passed(f"{self.subject} verified on {total} machine(s)")


class NonceAttestationCheck(_AttestationCheck):
    """Validate hardware passes a fresh nonce-based attestation (SEC22-01).

    Mirrors SPDM device attestation: a verifier issues a random challenge nonce
    and the host's device root of trust returns signed evidence. The host passes
    only when the nonce challenge is satisfied (``nonce_verified`` -- proving
    freshness, not a replay) and the returned evidence signature verifies
    (``attestation_signature_valid``). A host that cannot attest fails.

    Config:
        step_output: Step output containing per-machine attestation records.

    Step output (provider-neutral contract):
        success: bool
        platform: str
        machines_checked: int
        machines: list[dict]:
            machine_id: str
            attestation_supported: bool -- host participates in device attestation
            nonce_verified: bool -- the challenge nonce was satisfied (fresh)
            attestation_signature_valid: bool -- returned evidence verified
    """

    description: ClassVar[str] = "Check hardware passes a fresh nonce-based attestation"
    subject: ClassVar[str] = "Nonce attestation"
    subtest_prefix: ClassVar[str] = "nonce"

    def _evaluate(self, machine: dict[str, Any]) -> tuple[bool, str]:
        """Pass when the nonce challenge was satisfied and the evidence verified."""
        label = _machine_label(machine)
        if not machine.get("nonce_verified"):
            return False, f"{label}: nonce-based attestation not satisfied (stale, failed, or in progress)"
        if not machine.get("attestation_signature_valid"):
            return False, f"{label}: attestation evidence signature did not verify"
        return True, f"{label}: passed fresh nonce-based attestation"


class FirmwareAttestationCheck(_AttestationCheck):
    """Validate all firmware is signed and attested during boot (CNP09-02).

    Mirrors Measured Boot: firmware/bootloader/kernel measurements are extended
    into TPM PCRs during boot and verified against a golden bundle. The host
    passes when secure boot is enabled (``secure_boot_enabled`` -- firmware
    signatures are enforced, recorded in PCR 7) and the boot measurements were
    attested against the expected golden values (``boot_measurements_attested``).
    The recorded measured-boot state is surfaced for diagnostics. A host that
    cannot attest fails.

    Config:
        step_output: Step output containing per-machine attestation records
            (see ``NonceAttestationCheck`` for the shared schema; this check
            additionally reads ``secure_boot_enabled``,
            ``boot_measurements_attested``, and the optional
            ``measured_boot_state`` diagnostic).
    """

    description: ClassVar[str] = "Check all firmware is signed and attested during boot"
    subject: ClassVar[str] = "Firmware attestation"
    subtest_prefix: ClassVar[str] = "firmware"

    def _evaluate(self, machine: dict[str, Any]) -> tuple[bool, str]:
        """Pass when secure boot is enabled and boot measurements were attested."""
        label = _machine_label(machine)
        if not machine.get("secure_boot_enabled"):
            return False, f"{label}: secure boot is not enabled"
        if not machine.get("boot_measurements_attested"):
            state = machine.get("measured_boot_state") or "unknown"
            return False, f"{label}: boot measurements not attested against golden values (state: {state})"
        return True, f"{label}: secure boot enabled and boot measurements attested"
