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

"""Tenant-transition data-sanitization validations (requirement SEC21/SEC22).

Four provider-agnostic checks that assert a cloud does not hand a host to a
new tenant until it has been sanitized since the previous tenancy:

- ``MemorySanitizationCheck`` (SEC21-04): host (RAM) memory is sanitized
  between tenants.
- ``GpuMemorySanitizationCheck`` (SEC21-05): GPU/SRAM memory is sanitized
  between tenants (scoped to GPU-equipped hosts).
- ``FirmwareResetCheck`` (SEC21-06 / SEC22): TPM is cleared and BIOS/UEFI is
  recommitted during tenant transitions or hardware replacement.
- ``DiskSanitizationCheck`` (SEC21-02): storage/disk is sanitized on delete, so
  a prior tenant's on-disk data cannot leak to the next tenant.

All four share one provider-neutral signal: a host that has served a tenant
must pass through a dedicated *sanitizing* lifecycle stage before it becomes
allocatable to a new tenant again. The check inspects the host's recorded
state ``transitions`` and fails a host that went from ``in_use`` back to
``available`` without an intervening ``sanitizing`` stage, or that is offered
to new tenants while still bound to a prior tenant. They only inspect the
provider-neutral JSON a step script emits, so any provider that maps its
host lifecycle into the documented fields can reuse them.

The disk and memory checks gate on the same lifecycle signal: on platforms
like NICo a single between-tenant sanitizing stage performs RAM cleanup,
NVMe/HDD secure erase, the UEFI memory-overwrite check, InfiniBand cleanup,
and TPM/BIOS reset together, and a host is only returned to the allocatable
pool once that whole stage (including storage secure-erase) succeeds -- a
failed storage clean keeps the host out of the pool. So at the host-lifecycle
granularity these checks observe, storage sanitization on delete is confirmed
by the same gate; they remain separate test IDs per the requirements they map
to. A full low-level disk-remnant probe (write a marker, release, reallocate,
re-read the raw device) is out of scope for this lifecycle audit.
"""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation

# Provider-neutral lifecycle tokens used in each machine's ``transitions`` list.
IN_USE = "in_use"
SANITIZING = "sanitizing"
AVAILABLE = "available"


def _machine_label(machine: dict[str, Any]) -> str:
    """Human-facing identifier for a machine record."""
    return machine.get("machine_id") or "unknown"


def evaluate_sanitization(machine: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate the tenant-transition sanitization gate for one machine.

    Returns ``(passed, message)``. A machine passes when:

    * it has never served a tenant (nothing to sanitize), or
    * it is currently available and not bound to a prior tenant, and every
      recorded release passed through a ``sanitizing`` stage (the script sets
      ``sanitized`` from the host's state ``transitions``).

    A machine fails when it is offered to new tenants while still bound to a
    prior tenant, or when it returned to ``available`` after a tenancy without
    an intervening ``sanitizing`` stage.
    """
    label = _machine_label(machine)

    if not machine.get("served_tenant"):
        return True, f"{label}: no prior tenancy to sanitize (status {machine.get('status', 'unknown')})"

    if machine.get("stale_tenant_binding"):
        return False, f"{label}: available to new tenants while still bound to a prior tenant"

    if not machine.get("sanitized"):
        transitions = " -> ".join(str(t) for t in machine.get("transitions") or []) or "<none recorded>"
        return False, f"{label}: returned to the pool without sanitization (transitions: {transitions})"

    return True, f"{label}: sanitized between tenancies"


class _TenantSanitizationCheck(BaseValidation):
    """Shared machinery for the SEC21 tenant-transition sanitization checks.

    Subclasses set ``gpu_only`` and the ``subtest_prefix`` / summary wording.
    Each subclass keeps its own ``description`` and ``labels`` so it maps to a
    single test ID and can be toggled independently in a suite.
    """

    # Abstract base: the concrete subclasses below are the catalog entries.
    catalog_exclude: ClassVar[bool] = True
    timeout: ClassVar[int] = 120
    gpu_only: ClassVar[bool] = False
    subtest_prefix: ClassVar[str] = "machine"
    subject: ClassVar[str] = "Memory"

    def _select_machines(self, machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the machines this check applies to (all, or GPU-equipped)."""
        if self.gpu_only:
            return [m for m in machines if m.get("has_gpu")]
        return machines

    def run(self) -> None:
        """Validate that every in-scope machine was sanitized between tenancies."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"Sanitization step failed: {step_output.get('error', 'Unknown error')}")
            return

        machines = step_output.get("machines")
        if not isinstance(machines, list):
            self.set_failed("Sanitization step output is missing the 'machines' list")
            return

        scoped = self._select_machines(machines)
        if not scoped:
            scope = "GPU-equipped " if self.gpu_only else ""
            self.set_failed(f"No {scope}machines found in step output")
            return

        failed: dict[str, str] = {}
        served = 0
        for machine in scoped:
            if machine.get("served_tenant"):
                served += 1
            passed, message = evaluate_sanitization(machine)
            self.report_subtest(f"{self.subtest_prefix}_{_machine_label(machine)}", passed=passed, message=message)
            if not passed:
                failed[_machine_label(machine)] = message

        total = len(scoped)
        if failed:
            # Keep the summary concise: name a few offenders and a count. The
            # full per-machine reason (incl. transitions) is in the subtests.
            sample = ", ".join(list(failed)[:3])
            more = len(failed) - min(len(failed), 3)
            summary = f"{sample} (+{more} more)" if more else sample
            self.set_failed(f"{self.subject} failed for {len(failed)}/{total} machine(s): {summary}")
            return

        self.set_passed(f"{self.subject} verified on {total} machine(s) ({served} with a prior tenancy audited)")


class MemorySanitizationCheck(_TenantSanitizationCheck):
    """Validate host memory is sanitized between tenants (SEC21-04).

    Asserts that every managed host that has served a tenant is not returned to
    the allocatable pool until it has passed through the platform's sanitizing
    (host cleanup / memory-overwrite) lifecycle stage. A host that went from
    ``in_use`` straight back to ``available``, or that is still bound to a prior
    tenant while available, fails.

    Config:
        step_output: Step output containing per-machine sanitization records.

    Step output (from query_sanitization.py):
        success: bool
        platform: str
        site_id: str
        machines_checked: int
        machines: list[dict]:
            machine_id: str
            status: str -- neutral current lifecycle token
            available: bool -- allocatable to a new tenant now
            in_use: bool -- currently assigned to a tenant
            has_gpu: bool
            served_tenant: bool -- has hosted a tenant workload
            sanitized: bool -- every release passed through a sanitizing stage
            stale_tenant_binding: bool -- available but still bound to a prior tenant
            transitions: list[str] -- recent neutral lifecycle sequence
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check host memory is sanitized between tenants"
    subject: ClassVar[str] = "Host memory sanitization"
    subtest_prefix: ClassVar[str] = "memory"


class GpuMemorySanitizationCheck(_TenantSanitizationCheck):
    """Validate SRAM/GPU memory is sanitized between tenants (SEC21-05).

    Identical tenant-transition gate to ``MemorySanitizationCheck`` but scoped
    to GPU-equipped hosts, so it asserts that accelerator (GPU/SRAM) memory is
    scrubbed by the platform's sanitizing stage before a GPU host is offered to
    a new tenant. Fails when no GPU-equipped host is present (nothing to
    validate).

    Config:
        step_output: Step output containing per-machine sanitization records
            (see ``MemorySanitizationCheck`` for the schema; ``has_gpu`` selects
            the in-scope machines).
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check SRAM/GPU memory is sanitized between tenants"
    subject: ClassVar[str] = "GPU memory sanitization"
    subtest_prefix: ClassVar[str] = "gpu_memory"
    gpu_only: ClassVar[bool] = True


class FirmwareResetCheck(_TenantSanitizationCheck):
    """Validate TPM and BIOS are reset during tenant transitions (SEC21-06/SEC22).

    Uses the same sanitization-gate audit (TPM clear and BIOS/UEFI recommit run
    inside the platform's sanitizing stage) and additionally surfaces, per
    machine, the firmware identity (vendor / product / BIOS version) recorded
    after the transition as report-only evidence. BIOS *version* policy
    (minimum approved version per platform) remains the job of
    ``HostSoftwareCheck.bios_baselines`` / ``tpm_baselines`` (SEC22-02).

    Config:
        step_output: Step output containing per-machine sanitization records
            (see ``MemorySanitizationCheck``); also reads ``vendor``,
            ``product_name``, and ``bios_version`` for the firmware evidence
            subtest.
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check TPM/BIOS are reset during tenant transitions"
    subject: ClassVar[str] = "Firmware reset"
    subtest_prefix: ClassVar[str] = "firmware"

    def run(self) -> None:
        """Validate the reset gate, then report per-machine firmware identity."""
        super().run()

        # Only emit the report-only firmware-identity subtests when the gate
        # itself was evaluable (step succeeded and machines were present).
        step_output = self.config.get("step_output", {})
        machines = step_output.get("machines")
        if not step_output.get("success") or not isinstance(machines, list):
            return

        for machine in machines:
            label = _machine_label(machine)
            vendor = machine.get("vendor") or "unknown"
            product = machine.get("product_name") or "unknown"
            bios_version = machine.get("bios_version") or "unknown"
            self.report_subtest(
                f"{self.subtest_prefix}_{label}_identity",
                passed=True,
                message=f"{label}: {vendor} {product}, BIOS {bios_version}",
            )


class DiskSanitizationCheck(_TenantSanitizationCheck):
    """Validate storage is sanitized on delete between tenants (SEC21-02).

    Uses the same tenant-transition gate as ``MemorySanitizationCheck`` but is
    storage-framed. On platforms like NICo the between-tenant sanitizing stage
    (the machine ``Reset`` status) performs the NVMe/HDD secure erase, and a
    host is only returned to the allocatable pool (``Ready`` + usable) once that
    stage -- including the storage secure-erase -- has completed; a failed disk
    clean drives the host to a failure state (e.g. ``NVMECleanFailed``) and
    keeps it out of the pool. So asserting the host passed through the
    sanitizing stage before becoming allocatable is the host-lifecycle
    confirmation that its storage was sanitized on delete.

    The per-disk secure-erase result (Scout's ``nvme``/``hdd`` cleanup steps)
    is not exposed in the host-lifecycle JSON this check consumes, and a full
    low-level disk-remnant probe (write a marker, release, reallocate, re-read
    the raw device) is out of scope for this audit.

    Config:
        step_output: Step output containing per-machine sanitization records
            (see ``MemorySanitizationCheck``).
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check storage is sanitized on delete between tenants"
    subject: ClassVar[str] = "Storage sanitization"
    subtest_prefix: ClassVar[str] = "disk"
