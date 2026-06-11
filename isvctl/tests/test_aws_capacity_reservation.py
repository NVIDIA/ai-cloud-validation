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

"""Tests for AWS capacity reservation grouping script."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.context import Context
from isvctl.orchestrator.step_executor import StepExecutor

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_SECURITY_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "aws" / "config" / "security.yaml"
AWS_CAPACITY_SCRIPT = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "capacity" / "reservation_grouping.py"


def _load_capacity_script() -> ModuleType:
    """Load the AWS capacity reservation grouping script as a module."""
    if not AWS_CAPACITY_SCRIPT.exists():
        pytest.fail("AWS capacity reservation grouping script is not implemented")
    spec = importlib.util.spec_from_file_location("test_aws_capacity_reservation_grouping", AWS_CAPACITY_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_error(operation_name: str, code: str = "AccessDenied", message: str = "denied") -> ClientError:
    """Create a botocore ClientError for fake AWS client failures."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation_name)


class _FakeSts:
    """Fake STS client returning the caller account."""

    def __init__(self, account_id: str = "123456789012") -> None:
        """Initialize the fake account id."""
        self.account_id = account_id

    def get_caller_identity(self) -> dict[str, str]:
        """Return a fake caller identity document."""
        return {"Account": self.account_id}


class _FakeResourceGroups:
    """Fake AWS Resource Groups client."""

    def __init__(self, *, already_exists: bool = False) -> None:
        """Initialize recorded Resource Groups calls.

        When ``already_exists`` is set, ``create_group`` simulates a
        pre-existing group so the script resolves it via ``get_group``.
        """
        self.already_exists = already_exists
        self.created: list[str] = []
        self.deleted: list[str] = []

    def create_group(
        self,
        Name: str,
        Description: str,
        ResourceQuery: dict[str, str],
        Tags: dict[str, str],
    ) -> dict[str, Any]:
        """Record group creation and return a fake ARN."""
        assert Description
        assert ResourceQuery["Type"] == "TAG_FILTERS_1_0"
        assert Tags["CreatedBy"] == "isvtest"
        if self.already_exists:
            raise _client_error("CreateGroup", code="BadRequestException", message="group already exists")
        self.created.append(Name)
        return {"Group": {"GroupArn": f"arn:aws:resource-groups:us-west-2:123456789012:group/{Name}"}}

    def get_group(self, GroupName: str) -> dict[str, Any]:
        """Resolve a pre-existing group's ARN."""
        return {"Group": {"GroupArn": f"arn:aws:resource-groups:us-west-2:123456789012:group/{GroupName}"}}

    def delete_group(self, GroupName: str) -> None:
        """Record group deletion."""
        self.deleted.append(GroupName)


class _FakeEc2:
    """Fake EC2 client for capacity reservation grouping behavior."""

    def __init__(
        self,
        *,
        owner_id: str = "123456789012",
        match_criteria: str = "targeted",
        create_error: ClientError | None = None,
        create_errors_by_az: dict[str, ClientError] | None = None,
        reservations: list[dict[str, Any]] | None = None,
        expected_availability_zone: str = "us-west-2a",
        offered_availability_zones: list[str] | None = None,
    ) -> None:
        """Configure capacity reservation response behavior."""
        self.owner_id = owner_id
        self.match_criteria = match_criteria
        self.create_error = create_error
        self.create_errors_by_az = create_errors_by_az or {}
        self.reservations = reservations or []
        self.expected_availability_zone = expected_availability_zone
        self.offered_availability_zones = offered_availability_zones or ["us-west-2b"]
        self.cancelled: list[str] = []
        self.create_attempts: list[str] = []
        self._created_reservation: dict[str, Any] | None = None

    def describe_instance_type_offerings(self, LocationType: str, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return fake instance type offerings by AZ."""
        assert LocationType == "availability-zone"
        assert Filters == [{"Name": "instance-type", "Values": ["g4dn.metal"]}]
        return {"InstanceTypeOfferings": [{"Location": az} for az in self.offered_availability_zones]}

    def create_capacity_reservation(
        self,
        InstanceType: str,
        InstancePlatform: str,
        AvailabilityZone: str,
        InstanceCount: int,
        InstanceMatchCriteria: str,
        TagSpecifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a fake capacity reservation."""
        self.create_attempts.append(AvailabilityZone)
        if self.create_error:
            raise self.create_error
        if AvailabilityZone in self.create_errors_by_az:
            raise self.create_errors_by_az[AvailabilityZone]
        assert InstanceType == "g4dn.metal"
        assert InstancePlatform == "Linux/UNIX"
        assert AvailabilityZone == self.expected_availability_zone
        assert InstanceCount == 1
        assert InstanceMatchCriteria == "targeted"
        assert TagSpecifications[0]["ResourceType"] == "capacity-reservation"
        self._created_reservation = {
            "CapacityReservationId": "cr-123",
            "OwnerId": self.owner_id,
            "InstanceType": InstanceType,
            "InstanceMatchCriteria": self.match_criteria,
            "State": "active",
        }
        return {"CapacityReservation": self._created_reservation}

    def describe_capacity_reservations(
        self,
        CapacityReservationIds: list[str] | None = None,
        Filters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Resolve a reservation by id, or by the capacity group tag filter for teardown."""
        if CapacityReservationIds is not None:
            assert CapacityReservationIds == ["cr-123"]
            reservations = [self._created_reservation] if self._created_reservation else self.reservations
            return {
                "CapacityReservations": [
                    reservation
                    for reservation in reservations
                    if reservation and reservation.get("CapacityReservationId") in CapacityReservationIds
                ]
            }
        assert Filters and Filters[0]["Name"].startswith("tag:")
        return {"CapacityReservations": self.reservations}

    def cancel_capacity_reservation(self, CapacityReservationId: str) -> None:
        """Record capacity reservation cleanup."""
        self.cancelled.append(CapacityReservationId)


def _run_aws_capacity(
    *,
    ec2: _FakeEc2 | None = None,
    sts: _FakeSts | None = None,
    resource_groups: _FakeResourceGroups | None = None,
) -> dict[str, Any]:
    """Run the AWS capacity setup/validation step with fake clients."""
    module = _load_capacity_script()
    return module.run(
        ec2=ec2 or _FakeEc2(),
        sts=sts or _FakeSts(),
        resource_groups=resource_groups or _FakeResourceGroups(),
        region="us-west-2",
        instance_type="g4dn.metal",
        availability_zone="us-west-2a",
        resource_group_name="cap04-test",
        reservation_count=1,
    )


def _run_aws_teardown(
    *,
    ec2: _FakeEc2,
    resource_groups: _FakeResourceGroups,
    delete_group: bool,
    reservation_created: bool = False,
    created_reservation_id: str = "",
) -> dict[str, Any]:
    """Run the AWS capacity teardown step with fake clients."""
    module = _load_capacity_script()
    return module.run_teardown(
        ec2=ec2,
        resource_groups=resource_groups,
        resource_group_name="cap04-test",
        delete_group=delete_group,
        reservation_created=reservation_created,
        created_reservation_id=created_reservation_id,
    )


def test_aws_capacity_config_keeps_empty_availability_zone_value() -> None:
    """Default empty availability_zone must not leave argparse with a dangling flag."""
    config = RunConfig.model_validate(merge_yaml_files([AWS_SECURITY_CONFIG]))
    setup_step = next(
        step for step in config.commands["security"].steps if step.name == "capacity_reservation_grouping"
    )

    rendered = StepExecutor()._render_args(setup_step.args, Context(config))

    assert "--availability-zone" not in rendered
    assert "--availability-zone=" in rendered


def test_aws_topology_config_keeps_empty_tenant_id_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default empty tenant_id must not leave argparse with a dangling flag."""
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    config = RunConfig.model_validate(merge_yaml_files([AWS_SECURITY_CONFIG]))
    topology_step = next(
        step for step in config.commands["security"].steps if step.name == "topology_block_atomic_allocation"
    )

    rendered = StepExecutor()._render_args(topology_step.args, Context(config))

    assert "--tenant-id" not in rendered
    assert "--tenant-id=" in rendered


def test_aws_capacity_teardown_renders_without_setup_step_output() -> None:
    """Standalone teardown should still run when setup did not emit output."""
    config = RunConfig.model_validate(merge_yaml_files([AWS_SECURITY_CONFIG]))
    teardown_step = next(step for step in config.commands["security"].steps if step.name == "capacity_teardown")

    rendered = StepExecutor()._render_args(teardown_step.args, Context(config))

    assert "--teardown" in rendered
    assert "--reservation-created" not in rendered
    assert "--delete-group" not in rendered
    assert "--created-reservation-id=" in rendered


def test_aws_capacity_reservation_success_outputs_neutral_contract() -> None:
    """AWS capacity script emits grouped and pinned provider-neutral output."""
    ec2 = _FakeEc2()
    resource_groups = _FakeResourceGroups()

    result = _run_aws_capacity(ec2=ec2, resource_groups=resource_groups)

    assert result["success"] is True
    assert result["platform"] == "aws"
    assert result["reservation_id"] == "cr-123"
    assert result["reservation_created"] is True
    assert result["created_reservation_id"] == "cr-123"
    assert result["account_id"] == "123456789012"
    assert result["pinned"] is True
    assert result["isolation_enforced"] is True
    assert result["resource_group_created"] is True
    assert result["resources"] == [
        {
            "resource_id": "cr-123",
            "resource_type": "compute",
            "account_id": "123456789012",
            "pinned": True,
        },
        {
            "resource_id": "g4dn.metal",
            "resource_type": "instance_type",
            "account_id": "123456789012",
            "pinned": True,
        },
    ]
    # Resource creation no longer cleans up inline; teardown is a separate phase.
    assert ec2.cancelled == []
    assert resource_groups.deleted == []


def test_aws_capacity_setup_reports_preexisting_group_as_not_created() -> None:
    """Setup must flag a pre-existing group so teardown leaves it untouched."""
    result = _run_aws_capacity(resource_groups=_FakeResourceGroups(already_exists=True))

    assert result["success"] is True
    assert result["resource_group_created"] is False


def test_aws_capacity_setup_reports_existing_reservation_as_not_created() -> None:
    """Inspecting an existing reservation must not mark it as created by this run."""
    module = _load_capacity_script()
    ec2 = _FakeEc2(
        reservations=[
            {
                "CapacityReservationId": "cr-123",
                "OwnerId": "123456789012",
                "InstanceType": "g4dn.metal",
                "InstanceMatchCriteria": "targeted",
                "State": "active",
            }
        ]
    )

    result = module.run(
        ec2=ec2,
        sts=_FakeSts(),
        resource_groups=_FakeResourceGroups(),
        region="us-west-2",
        instance_type="g4dn.metal",
        availability_zone="us-west-2a",
        resource_group_name="cap04-test",
        reservation_count=1,
        reservation_id="cr-123",
    )

    assert result["success"] is True
    assert result["reservation_created"] is False
    assert result["created_reservation_id"] == ""


def test_aws_capacity_selects_az_from_instance_type_offerings_when_default_empty() -> None:
    """Default AZ selection should choose an AZ that offers the requested instance type."""
    module = _load_capacity_script()
    result = module.run(
        ec2=_FakeEc2(expected_availability_zone="us-west-2b"),
        sts=_FakeSts(),
        resource_groups=_FakeResourceGroups(),
        region="us-west-2",
        instance_type="g4dn.metal",
        availability_zone="",
        resource_group_name="cap04-test",
        reservation_count=1,
    )

    assert result["success"] is True


def test_aws_capacity_retries_supported_azs_after_capacity_shortage() -> None:
    """Default AZ selection should try another supported AZ after a capacity shortage."""
    module = _load_capacity_script()
    ec2 = _FakeEc2(
        create_errors_by_az={
            "us-west-2a": _client_error(
                "CreateCapacityReservation",
                code="InsufficientInstanceCapacity",
                message="Insufficient capacity.",
            )
        },
        expected_availability_zone="us-west-2b",
        offered_availability_zones=["us-west-2a", "us-west-2b"],
    )

    result = module.run(
        ec2=ec2,
        sts=_FakeSts(),
        resource_groups=_FakeResourceGroups(),
        region="us-west-2",
        instance_type="g4dn.metal",
        availability_zone="",
        resource_group_name="cap04-test",
        reservation_count=1,
    )

    assert result["success"] is True
    assert result["reservation_id"] == "cr-123"
    assert ec2.create_attempts == ["us-west-2a", "us-west-2b"]


def test_aws_capacity_teardown_cancels_reservations_and_deletes_created_group() -> None:
    """Teardown cancels tagged active reservations and deletes a group it created."""
    ec2 = _FakeEc2(reservations=[{"CapacityReservationId": "cr-123", "State": "active"}])
    resource_groups = _FakeResourceGroups()

    result = _run_aws_teardown(
        ec2=ec2,
        resource_groups=resource_groups,
        delete_group=True,
        reservation_created=True,
        created_reservation_id="cr-123",
    )

    assert result["success"] is True
    assert ec2.cancelled == ["cr-123"]
    assert resource_groups.deleted == ["cap04-test"]


def test_aws_capacity_teardown_preserves_same_group_reservations_not_created_by_run() -> None:
    """Teardown must not cancel same-tag reservations from another run."""
    ec2 = _FakeEc2(
        reservations=[
            {"CapacityReservationId": "cr-123", "State": "active"},
            {"CapacityReservationId": "cr-other", "State": "active"},
        ]
    )

    result = _run_aws_teardown(
        ec2=ec2,
        resource_groups=_FakeResourceGroups(),
        delete_group=False,
        reservation_created=True,
        created_reservation_id="cr-123",
    )

    assert result["success"] is True
    assert ec2.cancelled == ["cr-123"]


def test_aws_capacity_teardown_requires_reservation_created_flag() -> None:
    """A leaked or stale reservation id alone is not enough to authorize cleanup."""
    ec2 = _FakeEc2(reservations=[{"CapacityReservationId": "cr-123", "State": "active"}])

    result = _run_aws_teardown(
        ec2=ec2,
        resource_groups=_FakeResourceGroups(),
        delete_group=False,
        reservation_created=False,
        created_reservation_id="cr-123",
    )

    assert result["success"] is True
    assert ec2.cancelled == []


def test_aws_capacity_teardown_preserves_preexisting_group() -> None:
    """Teardown must not delete a Resource Group this run did not create."""
    ec2 = _FakeEc2(reservations=[{"CapacityReservationId": "cr-123", "State": "active"}])
    resource_groups = _FakeResourceGroups()

    result = _run_aws_teardown(
        ec2=ec2,
        resource_groups=resource_groups,
        delete_group=False,
        reservation_created=True,
        created_reservation_id="cr-123",
    )

    assert result["success"] is True
    assert ec2.cancelled == ["cr-123"]
    assert resource_groups.deleted == []


def test_aws_capacity_teardown_skips_inactive_reservations() -> None:
    """Teardown ignores reservations already in a terminal state."""
    ec2 = _FakeEc2(reservations=[{"CapacityReservationId": "cr-gone", "State": "cancelled"}])

    result = _run_aws_teardown(ec2=ec2, resource_groups=_FakeResourceGroups(), delete_group=True)

    assert result["success"] is True
    assert ec2.cancelled == []


def test_aws_capacity_standalone_teardown_sweeps_tagged_reservations() -> None:
    """Without a created-reservation id (standalone --phase teardown) every tagged
    reservation is cancelled so AWS_CAPACITY_SKIP_DESTROY resources are not leaked."""
    ec2 = _FakeEc2(reservations=[{"CapacityReservationId": "cr-123", "State": "active"}])

    result = _run_aws_teardown(
        ec2=ec2,
        resource_groups=_FakeResourceGroups(),
        delete_group=False,
        reservation_created=False,
        created_reservation_id="",
    )

    assert result["success"] is True
    assert ec2.cancelled == ["cr-123"]


def test_aws_capacity_reservation_fails_on_account_mismatch() -> None:
    """AWS capacity script fails when the reservation owner differs from the caller account."""
    result = _run_aws_capacity(ec2=_FakeEc2(owner_id="999999999999"))

    assert result["success"] is False
    assert result["account_id"] == "123456789012"
    assert result["resources"][0]["account_id"] == "999999999999"
    assert result["isolation_enforced"] is False
    assert "owner account" in result["error"]


def test_aws_capacity_reservation_fails_when_reservation_is_open() -> None:
    """AWS capacity script fails when capacity reservation matching is open instead of targeted."""
    result = _run_aws_capacity(ec2=_FakeEc2(match_criteria="open"))

    assert result["success"] is False
    assert result["pinned"] is False
    assert "targeted" in result["error"]


def test_aws_capacity_reservation_reports_api_failure() -> None:
    """AWS capacity script reports AWS API failures as structured JSON."""
    result = _run_aws_capacity(ec2=_FakeEc2(create_error=_client_error("CreateCapacityReservation")))

    assert result["success"] is False
    assert result["error_type"] == "access_denied"
    assert "denied" in result["error"]
