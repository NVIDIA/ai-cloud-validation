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

"""Capacity reservation validations."""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation

COUNT_FIELDS: tuple[str, ...] = ("compute", "network", "storage")
RESOURCE_FIELDS: tuple[str, ...] = (
    "resource_id",
    "resource_type",
    "tenant_id",
    "topology_block_id",
    "performance_domain",
    "isolation_boundary",
)


def _is_non_empty_string(value: object) -> bool:
    """Return True when ``value`` is a string with non-whitespace content."""
    return isinstance(value, str) and bool(value.strip())


def _is_non_negative_int(value: Any) -> bool:
    """Return whether value is a real non-negative integer, excluding bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _resource_label(resource: dict[str, Any]) -> str:
    """Return a stable resource label for error messages."""
    value = resource.get("resource_id")
    return value if isinstance(value, str) and value else "<unknown>"


class CapacityReservationGroupingCheck(BaseValidation):
    """Validate resources are grouped and pinned to one account or tenant.

    Config:
        step_output: Provider-neutral capacity reservation grouping output.
        min_resources: Minimum grouped resource count. Defaults to 1.

    Step output:
        success: False when the provider script failed.
        reservation_id: Reservation or allocation identifier.
        account_id: Account or tenant identifier that owns the reservation.
        resources: Grouped resources, each with account_id and pinned=true.
        pinned: True when the reservation uses targeted/pinned matching.
        isolation_enforced: True when the provider enforces the tenant/account boundary.
    """

    description: ClassVar[str] = "Check capacity reservation grouping and tenant pinning"

    def run(self) -> None:
        """Validate capacity reservation grouping evidence."""
        step_output = self.config.get("step_output", {})
        if not isinstance(step_output, dict):
            self.set_failed("step_output must be an object")
            return

        if step_output.get("success") is not True:
            error = step_output.get("error") or step_output.get("error_type") or "step reported failure"
            self.set_failed(f"Capacity reservation grouping step failed: {error}")
            return

        reservation_id = step_output.get("reservation_id")
        if not _is_non_empty_string(reservation_id):
            self.set_failed("Missing non-empty reservation_id in step output")
            return

        account_id = step_output.get("account_id")
        if not _is_non_empty_string(account_id):
            self.set_failed("Missing non-empty account_id in step output")
            return

        min_resources = self._parse_positive_int("min_resources", default=1)
        if min_resources is None:
            return

        resources = step_output.get("resources", [])
        if not isinstance(resources, list):
            self.set_failed("resources must be a list")
            return
        if len(resources) < min_resources:
            self.set_failed(f"Only {len(resources)} grouped resource(s), minimum {min_resources} required")
            return

        account_ids = self._resource_account_ids(resources)
        if account_ids is None:
            return
        unexpected_accounts = sorted(account_ids - {account_id})
        if unexpected_accounts:
            self.set_failed(
                f"Grouped resources must use account_id {account_id!r}; found {', '.join(unexpected_accounts)}"
            )
            return

        if step_output.get("pinned") is not True:
            self.set_failed("Capacity reservation must be pinned")
            return
        if not self._resources_are_pinned(resources):
            return

        if step_output.get("isolation_enforced") is not True:
            self.set_failed("Capacity reservation isolation_enforced must be true")
            return

        self.set_passed(
            f"Capacity reservation {reservation_id} groups {len(resources)} resource(s) pinned to account {account_id}"
        )

    def _resource_account_ids(self, resources: list[Any]) -> set[str] | None:
        """Return resource account ids, or fail when resources are malformed."""
        account_ids: set[str] = set()
        for index, resource in enumerate(resources):
            if not isinstance(resource, dict):
                self.set_failed(f"resources[{index}] must be an object")
                return None
            account_id = resource.get("account_id")
            if not _is_non_empty_string(account_id):
                self.set_failed(f"resources[{index}].account_id must be a non-empty string")
                return None
            account_ids.add(account_id)
        return account_ids

    def _resources_are_pinned(self, resources: list[Any]) -> bool:
        """Return True when every grouped resource carries pinned=true."""
        for index, resource in enumerate(resources):
            if not isinstance(resource, dict):
                self.set_failed(f"resources[{index}] must be an object")
                return False
            if resource.get("pinned") is not True:
                resource_id = resource.get("resource_id", f"resources[{index}]")
                self.set_failed(f"Grouped resource {resource_id!r} must be pinned")
                return False
        return True


class CapacityTopologyBlockAtomicAllocationCheck(BaseValidation):
    """Validate topology block atomic allocation.

    Config:
        step_output: Provider-neutral output from the
            ``topology_block_atomic_allocation`` step.
        min_resources: Minimum resource records expected in the block
            (default: 1).

    Step output:
        success: bool
        platform: str
        topology_block: dict
            block_id: str
            reservation_id: str
            tenant_id: str
            allocated_as_unit: bool
            partial_allocation: bool
            homogeneous: bool
            isolation_enforced: bool
            requested: {"compute": int, "network": int, "storage": int}
            allocated: {"compute": int, "network": int, "storage": int}
            resources: list[dict]
    """

    description: ClassVar[str] = "Check topology block capacity is allocated as one atomic unit"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate atomicity, resource counts, homogeneity, and isolation."""
        step_output = self.config.get("step_output", {})

        if not isinstance(step_output, dict):
            self.set_failed("step_output must be an object")
            return
        if not step_output.get("success"):
            self.set_failed(f"Capacity topology block step failed: {step_output.get('error', 'Unknown error')}")
            return

        block = step_output.get("topology_block")
        if not isinstance(block, dict):
            self.set_failed("Capacity step output is missing the 'topology_block' object")
            return

        min_resources = self._parse_positive_int("min_resources", default=1)
        if min_resources is None:
            return

        resources = block.get("resources")
        if not isinstance(resources, list) or not all(isinstance(resource, dict) for resource in resources):
            self.set_failed("topology_block.resources must be a list of objects")
            return
        if len(resources) < min_resources:
            self.set_failed(f"Expected at least {min_resources} topology block resource(s), got {len(resources)}")
            return

        failures: list[str] = []

        block_id = block.get("block_id")
        tenant_id = block.get("tenant_id")
        reservation_id = block.get("reservation_id")
        for key, value in (("block_id", block_id), ("tenant_id", tenant_id), ("reservation_id", reservation_id)):
            if not _is_non_empty_string(value):
                failures.append(f"topology_block.{key} must be a non-empty string")

        atomic_ok = block.get("allocated_as_unit") is True and block.get("partial_allocation") is False
        self.report_subtest(
            "atomic_allocation",
            passed=atomic_ok,
            message="block allocated as one unit" if atomic_ok else "block reported partial allocation",
        )
        if not atomic_ok:
            failures.append("topology block reported partial allocation")

        requested = block.get("requested")
        allocated = block.get("allocated")
        counts_ok = self._validate_counts(requested, allocated, resources, failures)
        self.report_subtest(
            "requested_counts",
            passed=counts_ok,
            message=(
                "allocated counts match requested counts and resource records"
                if counts_ok
                else "allocated counts do not match requested counts or resource records"
            ),
        )

        membership_ok = self._validate_resource_membership(resources, block_id, tenant_id, failures)
        self.report_subtest(
            "resource_membership",
            passed=membership_ok,
            message=(
                "all resources belong to the same tenant and topology block"
                if membership_ok
                else "resources span multiple tenants or topology blocks"
            ),
        )

        performance_domains = {resource.get("performance_domain") for resource in resources}
        performance_ok = block.get("homogeneous") is True and len(performance_domains) == 1
        self.report_subtest(
            "performance_homogeneity",
            passed=performance_ok,
            message=(
                "one performance domain across resources"
                if performance_ok
                else "multiple performance domains across resources"
            ),
        )
        if block.get("homogeneous") is not True:
            failures.append("topology_block.homogeneous must be true")
        if len(performance_domains) != 1:
            failures.append(
                f"multiple performance domains in topology block: {sorted(str(v) for v in performance_domains)}"
            )

        isolation_boundaries = {resource.get("isolation_boundary") for resource in resources}
        isolation_ok = block.get("isolation_enforced") is True and len(isolation_boundaries) == 1
        self.report_subtest(
            "isolation_boundary",
            passed=isolation_ok,
            message=(
                "one isolation boundary across resources"
                if isolation_ok
                else "multiple isolation boundaries across resources"
            ),
        )
        if block.get("isolation_enforced") is not True:
            failures.append("topology_block.isolation_enforced must be true")
        if len(isolation_boundaries) != 1:
            failures.append(
                f"multiple isolation boundaries in topology block: {sorted(str(v) for v in isolation_boundaries)}"
            )

        if failures:
            self.set_failed(f"Capacity topology block invariants violated: {'; '.join(failures)}")
            return

        self.set_passed(
            f"Validated atomic topology block {block_id} for tenant {tenant_id} with {len(resources)} resource(s)"
        )

    def _validate_counts(
        self,
        requested: Any,
        allocated: Any,
        resources: list[dict[str, Any]],
        failures: list[str],
    ) -> bool:
        """Validate requested, allocated, and resource-record counts."""
        if not isinstance(requested, dict) or not isinstance(allocated, dict):
            failures.append("topology_block.requested and topology_block.allocated must be objects")
            return False

        ok = True
        resource_counts = {
            name: sum(1 for resource in resources if resource.get("resource_type") == name) for name in COUNT_FIELDS
        }
        for name in COUNT_FIELDS:
            req = requested.get(name, 0)
            got = allocated.get(name, 0)
            if not _is_non_negative_int(req):
                failures.append(f"requested {name} must be a non-negative integer")
                ok = False
                continue
            if not _is_non_negative_int(got):
                failures.append(f"allocated {name} must be a non-negative integer")
                ok = False
                continue
            if got != req:
                failures.append(f"allocated {name} {got} != requested {name} {req}")
                ok = False
            if got != resource_counts[name]:
                failures.append(f"allocated {name} {got} != resource records {resource_counts[name]}")
                ok = False
        return ok

    def _validate_resource_membership(
        self,
        resources: list[dict[str, Any]],
        block_id: Any,
        tenant_id: Any,
        failures: list[str],
    ) -> bool:
        """Validate per-resource shape and membership in one block and tenant."""
        ok = True
        for resource in resources:
            label = _resource_label(resource)
            for field in RESOURCE_FIELDS:
                value = resource.get(field)
                if not _is_non_empty_string(value):
                    failures.append(f"{label} missing non-empty {field}")
                    ok = False
            if resource.get("tenant_id") != tenant_id:
                failures.append(f"{label} tenant_id {resource.get('tenant_id')} != {tenant_id}")
                ok = False
            if resource.get("topology_block_id") != block_id:
                failures.append(f"{label} topology_block_id {resource.get('topology_block_id')} != {block_id}")
                ok = False
        return ok
