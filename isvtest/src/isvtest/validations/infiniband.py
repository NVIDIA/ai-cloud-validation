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

"""InfiniBand fabric-security validations (requirement SDN04).

Two provider-agnostic checks that assert an InfiniBand fabric isolates tenants
and is hardened with the expected subnet-manager keys:

- ``IbTenantIsolationCheck`` (SDN04-04): every tenant's InfiniBand compute is
  scoped to its own P_Key partition. Because the subnet manager only permits
  traffic between ports that share a P_Key, distinct-P_Key-per-tenant is the
  fabric-side isolation boundary -- a host cannot bypass it. This asserts no
  P_Key is shared across tenants and no tenant partition rides the all-ports
  default management partition.
- ``IbKeysConfiguredCheck`` (SDN04-05): the InfiniBand security keys (P_Key,
  Management Key, and the OpenSM/SHARP subnet-manager keys) are configured.

Both validations only inspect provider-neutral JSON produced by a step script,
so any provider that emits the documented fields can reuse them.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from isvtest.core.validation import BaseValidation

# The InfiniBand security keys SDN04-05 expects to be configured, in the order
# they appear in the requirement. ``p_key`` is the partition key the subnet
# manager enforces; the rest are OpenSM / SHARP subnet-manager keys configured
# on the UFM host (see the InfiniBand setup runbook).
IB_KEY_NAMES: tuple[str, ...] = (
    "p_key",
    "management_key",
    "aggregation_management_key",
    "vendor_specific_key",
    "congestion_control_key",
    "node2node_key",
    "manager2node_key",
)

# UFM's default partition spans every port in the fabric (membership is
# restricted to "limited" by the runbook hardening). A tenant partition that
# reused this P_Key would not be isolated, so it is never a valid tenant key.
DEFAULT_PARTITION_PKEY: int = 0x7FFF

# An InfiniBand P_Key is the partition number in the low 15 bits; the top bit
# (0x8000) is the membership type (full vs limited member). Mask it off before
# comparing partition identity so the all-ports default (0x7fff limited /
# 0xffff full) is detected either way and a tenant partition seen as both
# 0x0001 (limited) and 0x8001 (full) is recognised as the same partition.
PKEY_BASE_MASK: int = 0x7FFF


def _normalize_pkey(value: Any) -> int | None:
    """Return a P_Key as an int, accepting hex ("0x1") or decimal strings.

    Returns ``None`` when the value is missing or not parseable so callers can
    treat an unallocated/garbage P_Key as "no key" rather than crashing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        return int(text, 16) if text.startswith("0x") else int(text, 10)
    except ValueError:
        return None


class IbTenantIsolationCheck(BaseValidation):
    """Validate InfiniBand tenant isolation via P_Key partitioning (SDN04-04).

    InfiniBand isolation is enforced by the subnet manager: a host port that is
    not a member of a P_Key cannot exchange traffic with any other member of
    that P_Key, regardless of physical connectivity. NICo models each isolation
    domain as a tenant-owned partition bound to a single P_Key, so verifying
    isolation reduces to verifying the partition-to-tenant mapping:

    * every partition carries a real P_Key (the subnet-manager enforcement
      handle);
    * every partition is owned by exactly one tenant (tenant-scoped);
    * no P_Key is shared by two different tenants -- a shared P_Key would let
      those tenants' compute communicate;
    * no tenant partition reuses the all-ports default management partition.

    Together these guarantee that compute dedicated to one tenant (e.g. NVIDIA)
    is isolated from other customers on the fabric.

    Config:
        step_output: Step output containing the InfiniBand partitions.
        min_partitions: Minimum number of partitions expected (default: 1).
        default_partition_pkey: P_Key of the all-ports default partition that a
            tenant partition must never reuse (default: 0x7fff).

    Step output (from query_ib_tenant_isolation.py):
        success: bool
        platform: str
        site_id: str
        partitions_checked: int
        partitions: list[dict]:
            name: str
            partition_key: str -- hex P_Key, e.g. "0x1"
            tenant_id: str -- owning tenant (empty when unscoped)
            status: str -- partition status (Ready, Provisioning, ...)
    """

    description: ClassVar[str] = "Check InfiniBand tenant isolation via per-tenant P_Key partitioning"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate per-tenant P_Key scoping and the absence of cross-tenant key sharing."""
        step_output = self.config.get("step_output", {})

        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "InfiniBand tenant isolation validation skipped")

        if not step_output.get("success"):
            self.set_failed(f"IB tenant isolation step failed: {step_output.get('error', 'Unknown error')}")
            return

        partitions = step_output.get("partitions")
        if not isinstance(partitions, list):
            self.set_failed("IB tenant isolation step output is missing the 'partitions' list")
            return

        min_partitions = self._parse_positive_int("min_partitions", default=1)
        if min_partitions is None:
            return

        if len(partitions) < min_partitions:
            self.set_failed(f"Expected at least {min_partitions} InfiniBand partition(s), got {len(partitions)}")
            return

        default_pkey = _normalize_pkey(self.config.get("default_partition_pkey", DEFAULT_PARTITION_PKEY))
        default_base = default_pkey & PKEY_BASE_MASK if default_pkey is not None else None

        # Maps a partition number (membership bit masked off) to the set of
        # distinct tenants that own a partition with that key. More than one
        # tenant per key is an isolation breach (both tenants' ports could talk
        # over the shared partition).
        pkey_tenants: dict[int, set[str]] = {}
        failed: list[str] = []

        for partition in partitions:
            label = partition.get("name") or partition.get("partition_key") or "unknown"
            pkey_raw = partition.get("partition_key")
            pkey = _normalize_pkey(pkey_raw)
            tenant = partition.get("tenant_id")
            tenant = tenant.strip() if isinstance(tenant, str) else ""

            problems: list[str] = []
            partition_number: int | None = None
            if pkey is None:
                problems.append("no P_Key allocated")
            else:
                partition_number = pkey & PKEY_BASE_MASK
                if default_base is not None and partition_number == default_base:
                    problems.append(f"reuses the default all-ports partition ({pkey_raw})")
            if not tenant:
                problems.append("not scoped to a tenant")

            if problems:
                self.report_subtest(
                    f"partition_{label}",
                    passed=False,
                    message=f"Partition {label}: {'; '.join(problems)}",
                )
                failed.append(f"{label} ({'; '.join(problems)})")
                continue

            # pkey/tenant are valid here -- record ownership for collision
            # detection, keyed by partition number so membership variants of the
            # same partition collapse together.
            assert partition_number is not None
            pkey_tenants.setdefault(partition_number, set()).add(tenant)
            self.report_subtest(
                f"partition_{label}",
                passed=True,
                message=f"Partition {label}: P_Key {pkey_raw} scoped to tenant {tenant}",
            )

        # A P_Key owned by more than one tenant means those tenants are not
        # isolated from each other on the fabric.
        shared = {pkey: tenants for pkey, tenants in pkey_tenants.items() if len(tenants) > 1}
        for pkey, tenants in shared.items():
            self.report_subtest(
                f"pkey_{pkey:#x}_exclusive",
                passed=False,
                message=f"P_Key {pkey:#x} is shared by {len(tenants)} tenants: {', '.join(sorted(tenants))}",
            )

        if failed or shared:
            issues: list[str] = []
            if failed:
                issues.append(f"{len(failed)} misconfigured partition(s): {', '.join(failed)}")
            if shared:
                collisions = ", ".join(f"{pkey:#x} -> {sorted(tenants)}" for pkey, tenants in shared.items())
                issues.append(f"{len(shared)} P_Key(s) shared across tenants: {collisions}")
            self.set_failed(f"InfiniBand tenant isolation not enforced: {'; '.join(issues)}")
            return

        tenant_count = len({t for tenants in pkey_tenants.values() for t in tenants})
        self.set_passed(
            f"{len(partitions)} InfiniBand partition(s) isolate {tenant_count} tenant(s) "
            f"with distinct, tenant-scoped P_Keys"
        )


class IbKeysConfiguredCheck(BaseValidation):
    """Validate the InfiniBand security keys are configured (SDN04-05).

    SDN04 requires the InfiniBand fabric to be hardened with the partition key
    plus the OpenSM / SHARP subnet-manager keys: P_Key, Management Key (M_Key),
    Aggregation Management Key (SHARP AM_Key), VendorSpecific Key, Congestion
    Control Key, Node2Node Key, and Manager2Node Key.

    This validation is provider-neutral: the step script reports, per key, a
    tri-state ``configured`` flag (``true`` = verified configured, ``false`` =
    verified NOT configured, ``null`` = could not observe via the available
    APIs) and the check asserts every *required* key is verified configured.
    A required key the script could not observe causes a skip rather than a
    false pass, mirroring the other evidence-gated security checks.

    The set of required keys is intentionally configurable via ``required_keys``
    because a provider can only enforce the keys its APIs surface. The P_Key is
    queryable from any partition API; the remaining subnet-manager keys live on
    the UFM host and are only observable when the provider integrates with UFM.

    Config:
        step_output: Step output containing the per-key configuration evidence.
        required_keys: Keys that must be verified configured (default: all of
            IB_KEY_NAMES). Providers narrow this to the keys they can observe.

    Step output (from query_ib_keys.py):
        success: bool
        platform: str
        site_id: str
        partitions_with_pkey: int -- partitions carrying a concrete P_Key
        keys: dict[str, dict]:
            <key_name>: {
                configured: bool | None,
                source: str,   -- where the evidence came from (e.g. "nico", "ufm")
                detail: str,   -- human-readable explanation
            }
    """

    description: ClassVar[str] = "Check InfiniBand security keys (P_Key, Management Key, ...) are configured"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate the required InfiniBand keys are verified configured."""
        step_output = self.config.get("step_output", {})

        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "InfiniBand key validation skipped")

        if not step_output.get("success"):
            self.set_failed(f"IB keys step failed: {step_output.get('error', 'Unknown error')}")
            return

        keys = step_output.get("keys")
        if not isinstance(keys, dict):
            self.set_failed("IB keys step output is missing the 'keys' object")
            return

        required = self.config.get("required_keys", list(IB_KEY_NAMES))
        if not isinstance(required, list) or not required:
            self.set_failed("`required_keys` must be a non-empty list")
            return

        # Concrete P_Key evidence: at least one partition must carry a P_Key.
        partitions_with_pkey = step_output.get("partitions_with_pkey")
        if not isinstance(partitions_with_pkey, int) or isinstance(partitions_with_pkey, bool):
            self.set_failed("IB keys step output is missing integer 'partitions_with_pkey'")
            return

        # Report a subtest for every key the script described (required or not)
        # so operators see the full posture, then gate pass/fail on the required
        # subset alone.
        not_configured: list[str] = []
        unverified: list[str] = []

        for name in sorted(keys):
            entry = keys[name] if isinstance(keys[name], dict) else {}
            configured = entry.get("configured")
            detail = entry.get("detail") or ""
            is_required = name in required

            if configured is True:
                message = f"{name}: configured ({detail})" if detail else f"{name}: configured"
                self.report_subtest(f"key_{name}", passed=True, message=message)
            elif configured is False:
                self.report_subtest(f"key_{name}", passed=False, message=f"{name}: NOT configured ({detail})")
                if is_required:
                    not_configured.append(name)
            else:
                # Unknown: surface as an informational skipped subtest.
                self.report_subtest(f"key_{name}", passed=False, skipped=True, message=f"{name}: unverified ({detail})")
                if is_required:
                    unverified.append(f"{name} ({detail})" if detail else name)

        missing_keys = [k for k in required if k not in keys]
        if missing_keys:
            self.set_failed(f"IB keys step output is missing required key(s): {', '.join(missing_keys)}")
            return

        if not_configured:
            self.set_failed(f"InfiniBand key(s) not configured: {', '.join(not_configured)}")
            return

        if partitions_with_pkey < 1:
            self.set_failed("No InfiniBand partition carries a P_Key; the partition key is not configured")
            return

        if unverified:
            pytest.skip(
                f"InfiniBand key verification incomplete; could not observe required key(s): {', '.join(unverified)}"
            )

        self.set_passed(
            f"All {len(required)} required InfiniBand key(s) configured "
            f"({partitions_with_pkey} partition(s) carry a P_Key)"
        )
