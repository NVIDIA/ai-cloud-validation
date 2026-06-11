# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the AWS capacity reservation topology block script."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT / "isvctl" / "configs" / "providers" / "aws" / "scripts" / "capacity" / "topology_block_atomic_allocation.py"
)


def _load_script() -> ModuleType:
    """Load the AWS capacity script as a module."""
    spec = importlib.util.spec_from_file_location("aws_capacity_atomic", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**overrides: Any) -> SimpleNamespace:
    """Build script args with valid defaults."""
    values = {
        "tenant_id": "123456789012",
        "topology_block_id": "isv-cap04-block",
        "requested_nodes": 2,
        "requested_network": 1,
        "requested_storage": 0,
        "instance_type": "g4dn.metal",
        "instance_platform": "Linux/UNIX",
        "availability_zone": "us-west-2a",
        "placement_group": "isv-cap04-pg",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _instance(
    instance_id: str, *, account_id: str = "123456789012", group_name: str = "isv-cap04-pg"
) -> dict[str, Any]:
    """Build a minimal EC2 instance description."""
    return {
        "InstanceId": instance_id,
        "InstanceType": "g4dn.metal",
        "Placement": {
            "AvailabilityZone": "us-west-2a",
            "GroupName": group_name,
        },
        "Tags": [
            {"Key": "OwnerAccount", "Value": account_id},
            {"Key": "CreatedBy", "Value": "isvtest"},
        ],
    }


class _FakeWaiter:
    """Fake EC2 waiter that records terminated instance waits."""

    def __init__(self, actions: list[tuple[str, Any]]) -> None:
        self._actions = actions

    def wait(self, **kwargs: Any) -> None:
        """Record the waiter call."""
        self._actions.append(("wait", kwargs))


class _FailingWaiter:
    """Fake EC2 waiter that records a wait and then fails."""

    def __init__(self, actions: list[tuple[str, Any]], exc: Exception) -> None:
        self._actions = actions
        self._exc = exc

    def wait(self, **kwargs: Any) -> None:
        """Record the waiter call and raise the configured exception."""
        self._actions.append(("wait", kwargs))
        raise self._exc


class _FakeEc2Cleanup:
    """Fake EC2 client for cleanup sequencing."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []

    def terminate_instances(self, **kwargs: Any) -> None:
        """Record termination."""
        self.actions.append(("terminate_instances", kwargs))

    def get_waiter(self, name: str) -> _FakeWaiter:
        """Return a fake waiter."""
        self.actions.append(("get_waiter", name))
        return _FakeWaiter(self.actions)

    def cancel_capacity_reservation(self, **kwargs: Any) -> None:
        """Record capacity reservation cancelation."""
        self.actions.append(("cancel_capacity_reservation", kwargs))

    def delete_placement_group(self, **kwargs: Any) -> None:
        """Record placement group deletion."""
        self.actions.append(("delete_placement_group", kwargs))


class _FakeEc2PlacementGroupEventuallyReleased:
    """Fake EC2 client where placement group deletion needs one retry."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []
        self.delete_attempts = 0

    def cancel_capacity_reservation(self, **kwargs: Any) -> None:
        """Record capacity reservation cancelation."""
        self.actions.append(("cancel_capacity_reservation", kwargs))

    def delete_placement_group(self, **kwargs: Any) -> None:
        """Fail once with in-use, then record successful placement-group deletion."""
        self.delete_attempts += 1
        self.actions.append(("delete_placement_group", kwargs))
        if self.delete_attempts == 1:
            raise ClientError(
                {"Error": {"Code": "InvalidPlacementGroup.InUse", "Message": "placement group is still in use"}},
                "DeletePlacementGroup",
            )


class _FakeEc2Reservation:
    """Fake EC2 client for creating a placement-group-bound capacity reservation."""

    def __init__(self) -> None:
        """Initialize the fake client."""
        self.capacity_reservation_kwargs: dict[str, Any] = {}

    def describe_placement_groups(self, GroupNames: list[str]) -> dict[str, Any]:
        """Return the placement group ARN for the requested group."""
        assert GroupNames == ["isv-cap04-pg"]
        return {
            "PlacementGroups": [
                {
                    "GroupName": "isv-cap04-pg",
                    "GroupArn": "arn:aws:ec2:us-west-2:123456789012:placement-group/isv-cap04-pg",
                }
            ]
        }

    def create_capacity_reservation(self, **kwargs: Any) -> dict[str, Any]:
        """Record the requested capacity reservation."""
        self.capacity_reservation_kwargs = kwargs
        return {"CapacityReservation": {"CapacityReservationId": "cr-123"}}


class _FakeEc2AzFallback:
    """Fake EC2 client where the first supported AZ has no capacity."""

    def __init__(self) -> None:
        """Initialize recorded actions and attempted reservation AZs."""
        self.actions: list[tuple[str, Any]] = []
        self.create_attempts: list[str] = []

    def describe_instance_type_offerings(self, LocationType: str, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return supported AZs for the requested instance type."""
        assert LocationType == "availability-zone"
        assert Filters == [{"Name": "instance-type", "Values": ["g4dn.metal"]}]
        return {
            "InstanceTypeOfferings": [
                {"Location": "us-west-2a"},
                {"Location": "us-west-2b"},
            ]
        }

    def create_placement_group(self, **kwargs: Any) -> None:
        """Record placement group creation."""
        self.actions.append(("create_placement_group", kwargs))

    def describe_placement_groups(self, GroupNames: list[str]) -> dict[str, Any]:
        """Return a placement group ARN."""
        self.actions.append(("describe_placement_groups", {"GroupNames": GroupNames}))
        return {
            "PlacementGroups": [
                {
                    "GroupName": "isv-cap04-pg",
                    "GroupArn": "arn:aws:ec2:us-west-2:123456789012:placement-group/isv-cap04-pg",
                }
            ]
        }

    def create_capacity_reservation(self, **kwargs: Any) -> dict[str, Any]:
        """Fail in us-west-2a and succeed in us-west-2b."""
        availability_zone = kwargs["AvailabilityZone"]
        self.create_attempts.append(availability_zone)
        self.actions.append(("create_capacity_reservation", kwargs))
        if availability_zone == "us-west-2a":
            raise ClientError(
                {"Error": {"Code": "InsufficientInstanceCapacity", "Message": "Insufficient capacity."}},
                "CreateCapacityReservation",
            )
        assert availability_zone == "us-west-2b"
        return {"CapacityReservation": {"CapacityReservationId": "cr-123"}}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        """Return launched instance IDs in the successful fallback AZ."""
        self.actions.append(("run_instances", kwargs))
        assert kwargs["Placement"]["AvailabilityZone"] == "us-west-2b"
        return {"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}

    def get_waiter(self, name: str) -> _FakeWaiter:
        """Return a fake waiter."""
        self.actions.append(("get_waiter", name))
        return _FakeWaiter(self.actions)

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """Return running instances in the successful fallback AZ."""
        self.actions.append(("describe_instances", {"InstanceIds": InstanceIds}))
        return {"Reservations": [{"Instances": [_instance("i-1"), _instance("i-2")]}]}

    def terminate_instances(self, **kwargs: Any) -> None:
        """Record termination."""
        self.actions.append(("terminate_instances", kwargs))

    def cancel_capacity_reservation(self, **kwargs: Any) -> None:
        """Record capacity reservation cancelation."""
        self.actions.append(("cancel_capacity_reservation", kwargs))

    def delete_placement_group(self, **kwargs: Any) -> None:
        """Record placement group deletion."""
        self.actions.append(("delete_placement_group", kwargs))


class _FakeEc2LaunchWaiterFailure:
    """Fake EC2 client where instances launch but the running waiter fails."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []

    def create_placement_group(self, **kwargs: Any) -> None:
        """Record placement group creation."""
        self.actions.append(("create_placement_group", kwargs))

    def describe_placement_groups(self, GroupNames: list[str]) -> dict[str, Any]:
        """Return a placement group ARN."""
        self.actions.append(("describe_placement_groups", {"GroupNames": GroupNames}))
        return {
            "PlacementGroups": [
                {
                    "GroupName": "isv-cap04-pg",
                    "GroupArn": "arn:aws:ec2:us-west-2:123456789012:placement-group/isv-cap04-pg",
                }
            ]
        }

    def create_capacity_reservation(self, **kwargs: Any) -> dict[str, Any]:
        """Record capacity reservation creation."""
        self.actions.append(("create_capacity_reservation", kwargs))
        return {"CapacityReservation": {"CapacityReservationId": "cr-123"}}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        """Return launched instance IDs."""
        self.actions.append(("run_instances", kwargs))
        return {"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}

    def get_waiter(self, name: str) -> _FakeWaiter | _FailingWaiter:
        """Fail only the running waiter."""
        self.actions.append(("get_waiter", name))
        if name == "instance_running":
            return _FailingWaiter(self.actions, TimeoutError("instance running timed out"))
        return _FakeWaiter(self.actions)

    def terminate_instances(self, **kwargs: Any) -> None:
        """Record termination."""
        self.actions.append(("terminate_instances", kwargs))

    def cancel_capacity_reservation(self, **kwargs: Any) -> None:
        """Record capacity reservation cancelation."""
        self.actions.append(("cancel_capacity_reservation", kwargs))

    def delete_placement_group(self, **kwargs: Any) -> None:
        """Record placement group deletion."""
        self.actions.append(("delete_placement_group", kwargs))


class _FakeEc2SuccessfulAllocationCleanupFailure:
    """Fake EC2 client where allocation succeeds but cleanup fails."""

    def __init__(self) -> None:
        """Initialize recorded actions."""
        self.actions: list[tuple[str, Any]] = []

    def create_placement_group(self, **kwargs: Any) -> None:
        """Record placement group creation."""
        self.actions.append(("create_placement_group", kwargs))

    def describe_placement_groups(self, GroupNames: list[str]) -> dict[str, Any]:
        """Return a placement group ARN."""
        self.actions.append(("describe_placement_groups", {"GroupNames": GroupNames}))
        return {
            "PlacementGroups": [
                {
                    "GroupName": "isv-cap04-pg",
                    "GroupArn": "arn:aws:ec2:us-west-2:123456789012:placement-group/isv-cap04-pg",
                }
            ]
        }

    def create_capacity_reservation(self, **kwargs: Any) -> dict[str, Any]:
        """Record capacity reservation creation."""
        self.actions.append(("create_capacity_reservation", kwargs))
        return {"CapacityReservation": {"CapacityReservationId": "cr-123"}}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        """Return launched instance IDs."""
        self.actions.append(("run_instances", kwargs))
        return {"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}

    def get_waiter(self, name: str) -> _FakeWaiter:
        """Return a fake waiter."""
        self.actions.append(("get_waiter", name))
        return _FakeWaiter(self.actions)

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """Return running instances after successful launch."""
        self.actions.append(("describe_instances", {"InstanceIds": InstanceIds}))
        return {"Reservations": [{"Instances": [_instance("i-1"), _instance("i-2")]}]}

    def terminate_instances(self, **kwargs: Any) -> None:
        """Fail termination after recording it."""
        self.actions.append(("terminate_instances", kwargs))
        raise RuntimeError("termination denied")

    def cancel_capacity_reservation(self, **kwargs: Any) -> None:
        """Record capacity reservation cancelation."""
        self.actions.append(("cancel_capacity_reservation", kwargs))

    def delete_placement_group(self, **kwargs: Any) -> None:
        """Record placement group deletion."""
        self.actions.append(("delete_placement_group", kwargs))


class _FakeStsSuccess:
    """Fake STS client that returns a stable account ID."""

    def get_caller_identity(self) -> dict[str, Any]:
        """Return the fake AWS account identity."""
        return {"Account": "123456789012"}


class _FakeStsFailure:
    """Fake STS client that fails account lookup."""

    def get_caller_identity(self) -> dict[str, Any]:
        """Raise the configured STS access error."""
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetCallerIdentity")


def test_build_contract_success() -> None:
    """AWS helper should emit the provider-neutral contract on full allocation."""
    module = _load_script()
    contract = module.build_contract(
        _args(),
        account_id="123456789012",
        reservation_id="cr-123",
        instances=[_instance("i-1"), _instance("i-2")],
    )

    assert contract["success"] is True
    block = contract["topology_block"]
    assert block["allocated_as_unit"] is True
    assert block["partial_allocation"] is False
    assert block["allocated"] == {"compute": 2, "network": 1, "storage": 0}
    assert {resource["performance_domain"] for resource in block["resources"]} == {"g4dn.metal/us-west-2a/isv-cap04-pg"}


def test_build_contract_detects_partial_allocation() -> None:
    """Fewer launched instances than requested should be partial allocation."""
    module = _load_script()
    contract = module.build_contract(
        _args(),
        account_id="123456789012",
        reservation_id="cr-123",
        instances=[_instance("i-1")],
    )

    assert contract["success"] is False
    assert contract["topology_block"]["allocated_as_unit"] is False
    assert contract["topology_block"]["partial_allocation"] is True


def test_build_contract_detects_mixed_placement_group() -> None:
    """Instances outside the placement group break performance homogeneity."""
    module = _load_script()
    contract = module.build_contract(
        _args(),
        account_id="123456789012",
        reservation_id="cr-123",
        instances=[_instance("i-1"), _instance("i-2", group_name="other-pg")],
    )

    assert contract["success"] is False
    assert contract["topology_block"]["homogeneous"] is False


def test_build_contract_fails_when_instance_owned_by_other_account() -> None:
    """isolation_enforced is derived from instance ownership, not asserted."""
    module = _load_script()
    contract = module.build_contract(
        _args(),
        account_id="123456789012",
        reservation_id="cr-123",
        instances=[_instance("i-1"), _instance("i-2", account_id="999999999999")],
    )

    assert contract["success"] is False
    assert contract["topology_block"]["isolation_enforced"] is False


def test_build_contract_does_not_claim_unsupported_resource_counts_allocated() -> None:
    """AWS contract counters must stay in sync with the resource records."""
    module = _load_script()
    contract = module.build_contract(
        _args(requested_network=2, requested_storage=1),
        account_id="123456789012",
        reservation_id="cr-123",
        instances=[_instance("i-1"), _instance("i-2")],
    )

    block = contract["topology_block"]
    assert contract["success"] is False
    assert block["allocated"] == {"compute": 2, "network": 1, "storage": 0}
    assert [resource["resource_type"] for resource in block["resources"]].count("network") == 1
    assert [resource["resource_type"] for resource in block["resources"]].count("storage") == 0


def test_cleanup_terminates_instances_before_canceling_reservation() -> None:
    """AWS cleanup should drain instances before removing reservation and placement group."""
    module = _load_script()
    ec2 = _FakeEc2Cleanup()

    errors = module.cleanup(
        ec2,
        instance_ids=["i-1", "i-2"],
        reservation_id="cr-123",
        placement_group="isv-cap04-pg",
    )

    assert errors == []
    assert ec2.actions == [
        ("terminate_instances", {"InstanceIds": ["i-1", "i-2"]}),
        ("get_waiter", "instance_terminated"),
        ("wait", {"InstanceIds": ["i-1", "i-2"]}),
        ("cancel_capacity_reservation", {"CapacityReservationId": "cr-123"}),
        ("delete_placement_group", {"GroupName": "isv-cap04-pg"}),
    ]


def test_cleanup_retries_placement_group_delete_after_capacity_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Placement group release is eventually consistent after reservation cancelation."""
    module = _load_script()
    ec2 = _FakeEc2PlacementGroupEventuallyReleased()
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    errors = module.cleanup(
        ec2,
        instance_ids=[],
        reservation_id="cr-123",
        placement_group="isv-cap04-pg",
    )

    assert errors == []
    assert ec2.actions == [
        ("cancel_capacity_reservation", {"CapacityReservationId": "cr-123"}),
        ("delete_placement_group", {"GroupName": "isv-cap04-pg"}),
        ("delete_placement_group", {"GroupName": "isv-cap04-pg"}),
    ]


def test_capacity_reservation_is_scoped_to_placement_group_arn() -> None:
    """AWS capacity reservation must be created inside the cluster placement group."""
    module = _load_script()
    ec2 = _FakeEc2Reservation()

    reservation_id = module._create_capacity_reservation(ec2, _args(), "123456789012")

    assert reservation_id == "cr-123"
    assert (
        ec2.capacity_reservation_kwargs["PlacementGroupArn"]
        == "arn:aws:ec2:us-west-2:123456789012:placement-group/isv-cap04-pg"
    )


def test_main_retries_supported_azs_after_capacity_shortage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Blank topology AZ should try another supported AZ after a capacity shortage."""
    module = _load_script()
    ec2 = _FakeEc2AzFallback()

    def fake_client(service: str, region_name: str) -> Any:
        """Return fake AWS clients by service name."""
        assert region_name == "us-west-2"
        if service == "ec2":
            return ec2
        if service == "sts":
            return _FakeStsSuccess()
        raise AssertionError(f"unexpected service: {service}")

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "topology_block_atomic_allocation.py",
            "--region",
            "us-west-2",
            "--topology-block-id",
            "isv-cap04-block",
            "--instance-type",
            "g4dn.metal",
            "--availability-zone",
            "",
            "--placement-group",
            "isv-cap04-pg",
            "--requested-nodes",
            "2",
            "--requested-network",
            "1",
            "--requested-storage",
            "0",
            "--ami-id",
            "ami-123",
        ],
    )

    exit_code = module.main()
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert ec2.create_attempts == ["us-west-2a", "us-west-2b"]


def test_main_emits_failure_json_when_sts_account_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """STS lookup failures should stay inside the provider JSON contract."""
    module = _load_script()

    def fake_client(service: str, region_name: str) -> Any:
        """Return fake AWS clients by service name."""
        assert region_name == "us-west-2"
        if service == "ec2":
            return _FakeEc2Cleanup()
        if service == "sts":
            return _FakeStsFailure()
        raise AssertionError(f"unexpected service: {service}")

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "topology_block_atomic_allocation.py",
            "--region",
            "us-west-2",
            "--topology-block-id",
            "isv-cap04-block",
            "--instance-type",
            "g4dn.metal",
            "--availability-zone",
            "us-west-2a",
            "--placement-group",
            "isv-cap04-pg",
            "--requested-nodes",
            "2",
            "--requested-network",
            "1",
            "--requested-storage",
            "0",
        ],
    )

    exit_code = module.main()
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["platform"] == "aws"
    assert payload["test_name"] == "topology_block_atomic_allocation"
    assert payload["error_type"] == "access_denied"


def test_main_cleans_up_launched_instances_when_running_waiter_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Instances must be cleanup-visible as soon as run_instances returns."""
    module = _load_script()
    ec2 = _FakeEc2LaunchWaiterFailure()

    def fake_client(service: str, region_name: str) -> Any:
        """Return fake AWS clients by service name."""
        assert region_name == "us-west-2"
        if service == "ec2":
            return ec2
        if service == "sts":
            return _FakeStsSuccess()
        raise AssertionError(f"unexpected service: {service}")

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "topology_block_atomic_allocation.py",
            "--region",
            "us-west-2",
            "--topology-block-id",
            "isv-cap04-block",
            "--instance-type",
            "g4dn.metal",
            "--availability-zone",
            "us-west-2a",
            "--placement-group",
            "isv-cap04-pg",
            "--requested-nodes",
            "2",
            "--requested-network",
            "1",
            "--requested-storage",
            "0",
            "--ami-id",
            "ami-123",
        ],
    )

    exit_code = module.main()
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["error_type"] == "unknown_error"
    assert ("terminate_instances", {"InstanceIds": ["i-1", "i-2"]}) in ec2.actions


def test_main_fails_when_cleanup_after_successful_allocation_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cleanup errors after a successful allocation should fail the step."""
    module = _load_script()
    ec2 = _FakeEc2SuccessfulAllocationCleanupFailure()

    def fake_client(service: str, region_name: str) -> Any:
        """Return fake AWS clients by service name."""
        assert region_name == "us-west-2"
        if service == "ec2":
            return ec2
        if service == "sts":
            return _FakeStsSuccess()
        raise AssertionError(f"unexpected service: {service}")

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "topology_block_atomic_allocation.py",
            "--region",
            "us-west-2",
            "--topology-block-id",
            "isv-cap04-block",
            "--instance-type",
            "g4dn.metal",
            "--availability-zone",
            "us-west-2a",
            "--placement-group",
            "isv-cap04-pg",
            "--requested-nodes",
            "2",
            "--requested-network",
            "1",
            "--requested-storage",
            "0",
            "--ami-id",
            "ami-123",
        ],
    )

    exit_code = module.main()
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["error_type"] == "cleanup_failed"
    assert payload["error"] == "Topology block cleanup failed"
    assert any("terminate_instances" in error for error in payload["cleanup_errors"])


@pytest.mark.parametrize(
    ("requested_network", "requested_storage", "expected_error"),
    [
        ("2", "0", "requested_network must be 0 or 1"),
        ("1", "1", "requested_storage must be 0"),
    ],
)
def test_main_rejects_unsupported_resource_counts_before_cloud_calls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    requested_network: str,
    requested_storage: str,
    expected_error: str,
) -> None:
    """Unsupported AWS resource counts should fail before creating cloud resources."""
    module = _load_script()

    def fake_client(service: str, region_name: str) -> Any:
        """Fail the test if validation reaches cloud client construction."""
        raise AssertionError(f"unexpected {service} client in {region_name}")

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "topology_block_atomic_allocation.py",
            "--region",
            "us-west-2",
            "--topology-block-id",
            "isv-cap04-block",
            "--instance-type",
            "g4dn.metal",
            "--availability-zone",
            "us-west-2a",
            "--placement-group",
            "isv-cap04-pg",
            "--requested-nodes",
            "2",
            "--requested-network",
            requested_network,
            "--requested-storage",
            requested_storage,
            "--ami-id",
            "ami-123",
        ],
    )

    exit_code = module.main()
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["error_type"] == "unknown_error"
    assert expected_error in payload["error"]


class _FakeEc2TeardownSweep:
    """Fake EC2 client that resolves CAP04 topology resources by tag for teardown."""

    def __init__(
        self,
        *,
        instances: list[dict[str, Any]] | None = None,
        reservations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._instances = instances or []
        self._reservations = reservations or []
        self.cancelled: list[str] = []
        self.terminated: list[list[str]] = []
        self.deleted_groups: list[str] = []
        self.filters: list[list[dict[str, Any]]] = []

    def describe_instances(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return tagged instances; record the filter for assertions."""
        self.filters.append(Filters)
        return {"Reservations": [{"Instances": self._instances}]}

    def describe_capacity_reservations(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return tagged capacity reservations."""
        return {"CapacityReservations": self._reservations}

    def terminate_instances(self, InstanceIds: list[str]) -> None:
        """Record terminated instances."""
        self.terminated.append(InstanceIds)

    def get_waiter(self, name: str) -> _FakeWaiter:
        """Return a no-op waiter."""
        return _FakeWaiter([])

    def cancel_capacity_reservation(self, CapacityReservationId: str) -> None:
        """Record cancelled reservations."""
        self.cancelled.append(CapacityReservationId)

    def delete_placement_group(self, GroupName: str) -> None:
        """Record deleted placement groups."""
        self.deleted_groups.append(GroupName)


def test_run_teardown_sweeps_tagged_instances_reservation_and_group() -> None:
    """Standalone teardown reclaims the instances, reservation, and placement group by tag."""
    module = _load_script()
    ec2 = _FakeEc2TeardownSweep(
        instances=[
            {"InstanceId": "i-1", "State": {"Name": "running"}},
            {"InstanceId": "i-2", "State": {"Name": "running"}},
        ],
        reservations=[{"CapacityReservationId": "cr-1", "State": "active"}],
    )

    result = module.run_teardown(ec2, topology_block_id="isv-cap04-block", placement_group="isv-cap04-pg")

    assert result["success"] is True
    assert ec2.terminated == [["i-1", "i-2"]]
    assert ec2.cancelled == ["cr-1"]
    assert ec2.deleted_groups == ["isv-cap04-pg"]
    assert ec2.filters[0] == [
        {"Name": "tag:CreatedBy", "Values": ["isvtest"]},
        {"Name": "tag:TestName", "Values": [module.TEST_NAME]},
        {"Name": "tag:TopologyBlock", "Values": ["isv-cap04-block"]},
    ]


def test_run_teardown_skips_terminated_instances_and_inactive_reservations() -> None:
    """Teardown ignores already-terminated instances and terminal reservations."""
    module = _load_script()
    ec2 = _FakeEc2TeardownSweep(
        instances=[
            {"InstanceId": "i-gone", "State": {"Name": "terminated"}},
            {"InstanceId": "i-live", "State": {"Name": "running"}},
        ],
        reservations=[{"CapacityReservationId": "cr-gone", "State": "cancelled"}],
    )

    result = module.run_teardown(ec2, topology_block_id="isv-cap04-block", placement_group="isv-cap04-pg")

    assert result["success"] is True
    assert ec2.terminated == [["i-live"]]
    assert ec2.cancelled == []
    assert ec2.deleted_groups == ["isv-cap04-pg"]


def test_main_teardown_is_noop_when_skip_destroy_is_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--teardown --skip-destroy` preserves resources without any AWS calls."""
    module = _load_script()

    def fake_client(service: str, region_name: str) -> Any:
        """Fail if teardown touches AWS while skip-destroy is set."""
        raise AssertionError(f"unexpected {service} client in {region_name}")

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "topology_block_atomic_allocation.py",
            "--teardown",
            "--skip-destroy",
            "--region",
            "us-west-2",
            "--topology-block-id",
            "isv-cap04-block",
            "--instance-type",
            "g4dn.metal",
            "--availability-zone",
            "us-west-2a",
            "--placement-group",
            "isv-cap04-pg",
            "--requested-nodes",
            "2",
            "--requested-network",
            "1",
            "--requested-storage",
            "0",
        ],
    )

    exit_code = module.main()
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["skipped"] == "AWS_CAPACITY_SKIP_DESTROY set"
