# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for capacity reservation checks in the security suite and provider wiring."""

from __future__ import annotations

from pathlib import Path

from isvctl.config.merger import merge_yaml_files

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "isvctl" / "configs"


def test_security_suite_defines_capacity_validations() -> None:
    """The provider-neutral security suite should expose capacity reservation checks."""
    suite = merge_yaml_files([CONFIGS / "suites" / "security.yaml"])
    validations = suite["tests"]["validations"]

    assert "capacity_reservation_grouping" in validations
    grouping = validations["capacity_reservation_grouping"]
    assert grouping["checks"] == {
        "CapacityReservationGroupingCheck": {
            "step": "capacity_reservation_grouping",
            "min_resources": "{{min_resources}}",
        }
    }

    assert "topology_block_atomic_allocation" in validations
    topology = validations["topology_block_atomic_allocation"]
    assert topology["step"] == "topology_block_atomic_allocation"
    assert topology["checks"] == {
        "CapacityTopologyBlockAtomicAllocationCheck": {
            "min_resources": 2,
        }
    }


def test_my_isv_security_config_wires_capacity_steps() -> None:
    """The my-isv security example should run the capacity reservation steps."""
    merged = merge_yaml_files([CONFIGS / "providers" / "my-isv" / "config" / "security.yaml"])
    steps = merged["commands"]["security"]["steps"]

    grouping = next(item for item in steps if item["name"] == "capacity_reservation_grouping")
    assert grouping["phase"] == "test"
    assert grouping["command"] == "python ../scripts/capacity/reservation_grouping.py"
    assert grouping["requires_available_validations"] == ["CapacityReservationGroupingCheck"]
    assert "--account-id" in grouping["args"]
    assert "{{tenant_id}}" in grouping["args"]

    step = next(item for item in steps if item["name"] == "topology_block_atomic_allocation")

    assert step["phase"] == "test"
    assert step["command"] == "python ../scripts/capacity/topology_block_atomic_allocation.py"
    assert step["requires_available_validations"] == ["CapacityTopologyBlockAtomicAllocationCheck"]
    assert "--tenant-id" in step["args"]
    assert "{{tenant_id}}" in step["args"]
    assert "--requested-compute" in step["args"]
    assert "{{requested_compute}}" in step["args"]


def test_aws_security_config_wires_capacity_steps() -> None:
    """The AWS security config should wire both capacity reservation steps to AWS scripts."""
    merged = merge_yaml_files([CONFIGS / "providers" / "aws" / "config" / "security.yaml"])
    steps = merged["commands"]["security"]["steps"]
    settings = merged["tests"]["settings"]
    grouping = next(item for item in steps if item["name"] == "capacity_reservation_grouping")
    teardown = next(item for item in steps if item["name"] == "capacity_teardown")
    topology_teardown = next(item for item in steps if item["name"] == "topology_block_teardown")

    assert grouping["command"] == "python3 ../scripts/capacity/reservation_grouping.py"
    assert grouping["requires_available_validations"] == ["CapacityReservationGroupingCheck"]
    assert teardown["requires_available_validations"] == ["CapacityReservationGroupingCheck"]
    assert topology_teardown["phase"] == "teardown"
    assert topology_teardown["command"] == "python3 ../scripts/capacity/topology_block_atomic_allocation.py"
    assert "--teardown" in topology_teardown["args"]
    assert "{{topology_skip_destroy_flag}}" in topology_teardown["args"]

    step = next(item for item in steps if item["name"] == "topology_block_atomic_allocation")

    assert step["command"] == "python3 ../scripts/capacity/topology_block_atomic_allocation.py"
    assert step["requires_available_validations"] == ["CapacityTopologyBlockAtomicAllocationCheck"]
    assert "--capacity-reservation-id" not in step["args"]
    assert "--instance-type" in step["args"]
    assert "{{capacity_instance_type}}" in step["args"]
    assert "--placement-group" in step["args"]
    assert "{{placement_group}}" in step["args"]
    assert "{{topology_skip_destroy_flag}}" in step["args"]
    assert settings["topology_availability_zone"] == "{{env.AWS_AVAILABILITY_ZONE | default('', true)}}"
