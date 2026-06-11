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

"""Tests for capacity reservation validations."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.capacity import CapacityTopologyBlockAtomicAllocationCheck


def _validation_cls() -> Any:
    """Return the capacity reservation validation class under test."""
    try:
        # Local import turns missing-check regressions into a targeted pytest failure.
        from isvtest.validations.capacity import CapacityReservationGroupingCheck
    except ModuleNotFoundError:
        pytest.fail("CapacityReservationGroupingCheck is not implemented")
    return CapacityReservationGroupingCheck


def _capacity_output(**overrides: Any) -> dict[str, Any]:
    """Build a passing capacity reservation grouping step output."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "aws",
        "reservation_id": "cr-123",
        "account_id": "acct-123",
        "resources": [
            {
                "resource_id": "cr-123",
                "resource_type": "compute",
                "account_id": "acct-123",
                "pinned": True,
            },
            {
                "resource_id": "rg-123",
                "resource_type": "network",
                "account_id": "acct-123",
                "pinned": True,
            },
        ],
        "pinned": True,
        "isolation_enforced": True,
    }
    output.update(overrides)
    return output


def _run_capacity_check(step_output: dict[str, Any], **config_overrides: Any) -> dict[str, Any]:
    """Execute the capacity reservation grouping check with the supplied step output."""
    config = {"step_output": step_output}
    config.update(config_overrides)
    return _validation_cls()(config=config).execute()


def test_capacity_reservation_grouping_passes_for_grouped_pinned_resources() -> None:
    """Capacity grouping passes when all resources are pinned to one account."""
    result = _run_capacity_check(_capacity_output())

    assert result["passed"] is True
    assert "cr-123" in result["output"]
    assert "2 resource(s)" in result["output"]


def test_capacity_reservation_grouping_requires_reservation_id() -> None:
    """Capacity grouping fails when the reservation id is missing."""
    result = _run_capacity_check(_capacity_output(reservation_id=""))

    assert result["passed"] is False
    assert "reservation_id" in result["error"]


def test_capacity_reservation_grouping_requires_minimum_resource_count() -> None:
    """Capacity grouping fails when fewer than min_resources are grouped."""
    result = _run_capacity_check(_capacity_output(resources=[]), min_resources=1)

    assert result["passed"] is False
    assert "minimum 1" in result["error"]


def test_capacity_reservation_grouping_rejects_mixed_account_ids() -> None:
    """Capacity grouping fails when grouped resources belong to different accounts."""
    resources = _capacity_output()["resources"]
    resources[1] = {**resources[1], "account_id": "acct-999"}

    result = _run_capacity_check(_capacity_output(resources=resources))

    assert result["passed"] is False
    assert "acct-999" in result["error"]


def test_capacity_reservation_grouping_requires_pinned_reservation() -> None:
    """Capacity grouping fails when the reservation is not pinned."""
    result = _run_capacity_check(_capacity_output(pinned=False))

    assert result["passed"] is False
    assert "pinned" in result["error"]


def test_capacity_reservation_grouping_requires_isolation_enforced() -> None:
    """Capacity grouping fails when the provider does not enforce isolation."""
    result = _run_capacity_check(_capacity_output(isolation_enforced=False))

    assert result["passed"] is False
    assert "isolation" in result["error"]


def test_capacity_reservation_grouping_propagates_step_failure() -> None:
    """Capacity grouping fails with the script error when the step reports failure."""
    result = _run_capacity_check(_capacity_output(success=False, error="API rejected the reservation request"))

    assert result["passed"] is False
    assert "API rejected" in result["error"]


def test_capacity_reservation_grouping_requires_explicit_success() -> None:
    """Capacity grouping fails when the step omits the success flag."""
    output = _capacity_output()
    output.pop("success")

    result = _run_capacity_check(output)

    assert result["passed"] is False
    assert "step reported failure" in result["error"]


def _topology_resource(
    resource_id: str,
    *,
    resource_type: str = "compute",
    tenant_id: str = "tenant-a",
    block_id: str = "block-a",
    performance_domain: str = "pd-a",
    isolation_boundary: str = "tenant-a",
) -> dict[str, Any]:
    """Build one topology-block resource record."""
    return {
        "resource_id": resource_id,
        "resource_type": resource_type,
        "tenant_id": tenant_id,
        "topology_block_id": block_id,
        "performance_domain": performance_domain,
        "isolation_boundary": isolation_boundary,
    }


def _topology_output(
    *,
    success: bool = True,
    resources: list[dict[str, Any]] | None = None,
    requested: dict[str, int] | None = None,
    allocated: dict[str, int] | None = None,
    block_overrides: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a step output with valid defaults."""
    if resources is None:
        resources = [
            _topology_resource("node-1"),
            _topology_resource("node-2"),
            _topology_resource("fabric-1", resource_type="network"),
        ]
    block: dict[str, Any] = {
        "block_id": "block-a",
        "reservation_id": "reservation-a",
        "tenant_id": "tenant-a",
        "allocated_as_unit": True,
        "partial_allocation": False,
        "homogeneous": True,
        "isolation_enforced": True,
        "requested": requested or {"compute": 2, "network": 1, "storage": 0},
        "allocated": allocated or {"compute": 2, "network": 1, "storage": 0},
        "resources": resources,
    }
    if block_overrides:
        block.update(block_overrides)
    return {
        "success": success,
        "platform": "test",
        "test_name": "topology_block_atomic_allocation",
        "topology_block": block,
        "error": error,
    }


class TestCapacityTopologyBlockAtomicAllocationCheck:
    """Tests for topology block atomic allocation validation."""

    def test_valid_atomic_block_passes(self) -> None:
        """A complete homogeneous block with one isolation boundary passes."""
        check = CapacityTopologyBlockAtomicAllocationCheck(config={"step_output": _topology_output()})

        check.run()

        assert check._passed is True, check._error
        assert "atomic topology block" in check._output
        assert {result["name"] for result in check._subtest_results} == {
            "atomic_allocation",
            "requested_counts",
            "resource_membership",
            "performance_homogeneity",
            "isolation_boundary",
        }
        assert all(result["passed"] for result in check._subtest_results)

    def test_step_failure_propagates(self) -> None:
        """A failed provider step should fail with the provider error."""
        check = CapacityTopologyBlockAtomicAllocationCheck(
            config={"step_output": _topology_output(success=False, error="capacity API denied request")}
        )

        check.run()

        assert check._passed is False
        assert "capacity API denied request" in check._error

    def test_partial_allocation_fails(self) -> None:
        """A block reported as partial is not atomic."""
        output = _topology_output(
            block_overrides={
                "allocated_as_unit": False,
                "partial_allocation": True,
            }
        )
        check = CapacityTopologyBlockAtomicAllocationCheck(config={"step_output": output})

        check.run()

        assert check._passed is False
        assert "partial allocation" in check._error
        atomic = next(result for result in check._subtest_results if result["name"] == "atomic_allocation")
        assert atomic["passed"] is False

    def test_requested_allocated_mismatch_fails(self) -> None:
        """Allocated counts must exactly match requested counts."""
        output = _topology_output(allocated={"compute": 1, "network": 1, "storage": 0})
        check = CapacityTopologyBlockAtomicAllocationCheck(config={"step_output": output})

        check.run()

        assert check._passed is False
        assert "allocated compute 1 != requested compute 2" in check._error
        counts = next(result for result in check._subtest_results if result["name"] == "requested_counts")
        assert counts["passed"] is False
        # A failed subtest must not report a success-phrased message.
        assert "do not match" in counts["message"]

    def test_allocated_counts_must_match_resource_records(self) -> None:
        """Allocated counters cannot claim resources missing from the resource list."""
        output = _topology_output(allocated={"compute": 2, "network": 2, "storage": 0})
        check = CapacityTopologyBlockAtomicAllocationCheck(config={"step_output": output})

        check.run()

        assert check._passed is False
        assert "allocated network 2 != resource records 1" in check._error

    def test_mixed_performance_domains_fail(self) -> None:
        """All resources in an atomic topology block must share one performance domain."""
        resources = [
            _topology_resource("node-1"),
            _topology_resource("node-2", performance_domain="pd-b"),
            _topology_resource("fabric-1", resource_type="network"),
        ]
        check = CapacityTopologyBlockAtomicAllocationCheck(
            config={"step_output": _topology_output(resources=resources)}
        )

        check.run()

        assert check._passed is False
        assert "multiple performance domains" in check._error

    def test_mixed_isolation_boundaries_fail(self) -> None:
        """All resources in the block must share one isolation boundary."""
        resources = [
            _topology_resource("node-1"),
            _topology_resource("node-2", isolation_boundary="tenant-b"),
            _topology_resource("fabric-1", resource_type="network"),
        ]
        check = CapacityTopologyBlockAtomicAllocationCheck(
            config={"step_output": _topology_output(resources=resources)}
        )

        check.run()

        assert check._passed is False
        assert "multiple isolation boundaries" in check._error

    def test_resource_outside_tenant_fails(self) -> None:
        """A resource assigned to another tenant breaks the security boundary."""
        resources = [
            _topology_resource("node-1"),
            _topology_resource("node-2", tenant_id="tenant-b"),
            _topology_resource("fabric-1", resource_type="network"),
        ]
        check = CapacityTopologyBlockAtomicAllocationCheck(
            config={"step_output": _topology_output(resources=resources)}
        )

        check.run()

        assert check._passed is False
        assert "node-2 tenant_id tenant-b != tenant-a" in check._error

    def test_resource_outside_block_fails(self) -> None:
        """Every resource must identify the same topology block."""
        resources = [
            _topology_resource("node-1"),
            _topology_resource("node-2", block_id="block-b"),
            _topology_resource("fabric-1", resource_type="network"),
        ]
        check = CapacityTopologyBlockAtomicAllocationCheck(
            config={"step_output": _topology_output(resources=resources)}
        )

        check.run()

        assert check._passed is False
        assert "node-2 topology_block_id block-b != block-a" in check._error

    def test_invalid_min_resources_fails(self) -> None:
        """Config validation rejects non-positive min_resources values."""
        check = CapacityTopologyBlockAtomicAllocationCheck(
            config={
                "step_output": _topology_output(),
                "min_resources": 0,
            }
        )

        check.run()

        assert check._passed is False
        assert "min_resources must be >= 1" in check._error
