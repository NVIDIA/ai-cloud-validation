#!/usr/bin/env python3
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

"""AWS capacity reservation topology block atomic allocation reference implementation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import candidate_availability_zones, get_ubuntu_ami
from common.errors import TRANSIENT_AWS_CODES, classify_aws_error

TEST_NAME = "topology_block_atomic_allocation"
# Capacity-exhaustion codes the shared classifier does not model; everything
# else delegates to ``classify_aws_error`` so this check shares the suite's
# error vocabulary instead of inventing its own.
CAPACITY_UNAVAILABLE_CODES = frozenset(
    {
        "InsufficientInstanceCapacity",
        "InsufficientReservedInstanceCapacity",
        "InstanceLimitExceeded",
        "MaxSpotInstanceCountExceeded",
    }
)
INSTANCE_ALREADY_GONE_CODES = frozenset({"InvalidInstanceID.NotFound"})
CAPACITY_RESERVATION_ALREADY_GONE_CODES = frozenset({"InvalidCapacityReservationId.NotFound"})
PLACEMENT_GROUP_ALREADY_GONE_CODES = frozenset({"InvalidPlacementGroup.Unknown"})
PLACEMENT_GROUP_RETRYABLE_DELETE_CODES = TRANSIENT_AWS_CODES | frozenset(
    {
        "DependencyViolation",
        "InvalidPlacementGroup.InUse",
    }
)
PLACEMENT_GROUP_DELETE_ATTEMPTS = 10
PLACEMENT_GROUP_DELETE_BACKOFF_SECONDS = 3.0
# Reservation/instance states that need no teardown action.
INACTIVE_RESERVATION_STATES = frozenset({"cancelled", "expired", "failed"})
TERMINAL_INSTANCE_STATES = frozenset({"terminated"})


def _positive_int(value: str) -> int:
    """Parse a positive integer argparse value."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return parsed


def _non_negative_int(value: str) -> int:
    """Parse a non-negative integer argparse value."""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return parsed


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify an error via the shared AWS classifier, flagging capacity exhaustion.

    Reuses :func:`classify_aws_error` so this check reports the same
    ``error_type`` vocabulary as the rest of the suite, and only adds the
    capacity-specific ``capacity_unavailable`` type the shared helper does not
    model.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in CAPACITY_UNAVAILABLE_CODES:
            return "capacity_unavailable", classify_aws_error(exc)[1]
    return classify_aws_error(exc)


def _cleanup_call(
    fn: Any,
    *,
    action: str,
    gone_codes: frozenset[str] = frozenset(),
    retry_codes: frozenset[str] = TRANSIENT_AWS_CODES,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    **kwargs: Any,
) -> str | None:
    """Run one cleanup API call, retrying transient or eventually-consistent failures."""
    for attempt in range(1, attempts + 1):
        try:
            fn(**kwargs)
            return None
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in gone_codes:
                return None
            if code in retry_codes and attempt < attempts:
                time.sleep(backoff_seconds * attempt)
                continue
            return f"{action}: {classify_aws_error(exc)[1]}"
        except Exception as exc:
            return f"{action}: {type(exc).__name__}: {exc}"


def _failure(error: str, *, error_type: str) -> dict[str, Any]:
    """Build a concise failure payload."""
    return {
        "success": False,
        "platform": "aws",
        "test_name": TEST_NAME,
        "error_type": error_type,
        "error": error,
    }


def _validate_supported_resource_counts(args: argparse.Namespace) -> None:
    """Reject AWS resource counts this reference implementation cannot model."""
    if args.requested_network > 1:
        raise ValueError("requested_network must be 0 or 1 for AWS topology block allocations")
    if args.requested_storage != 0:
        raise ValueError("requested_storage must be 0 for AWS topology block allocations")


def _is_capacity_unavailable(error: Exception) -> bool:
    """Return whether an AWS error indicates no allocatable capacity in this AZ."""
    if not isinstance(error, ClientError):
        return False
    return str(error.response.get("Error", {}).get("Code", "")) in CAPACITY_UNAVAILABLE_CODES


def _instance_owner_account(instance: dict[str, Any]) -> str | None:
    """Return the OwnerAccount tag an instance was launched with, if present."""
    for tag in instance.get("Tags", []):
        if tag.get("Key") == "OwnerAccount":
            return tag.get("Value")
    return None


def _tags(args: argparse.Namespace, account_id: str) -> list[dict[str, str]]:
    """Return common AWS resource tags for CAP resources."""
    return [
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "TestName", "Value": TEST_NAME},
        {"Key": "OwnerAccount", "Value": account_id},
        {"Key": "TopologyBlock", "Value": args.topology_block_id},
    ]


def build_contract(
    args: argparse.Namespace,
    *,
    account_id: str,
    reservation_id: str,
    instances: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the provider-neutral capacity reservation contract from EC2 instance data."""
    requested = {
        "compute": args.requested_nodes,
        "network": args.requested_network,
        "storage": args.requested_storage,
    }
    full_compute_allocation = len(instances) == args.requested_nodes
    allocated = {
        "compute": len(instances),
        "network": 1 if full_compute_allocation and args.requested_network > 0 else 0,
        "storage": 0,
    }
    performance_domains = {
        f"{instance.get('InstanceType', '')}/{instance.get('Placement', {}).get('AvailabilityZone', '')}/"
        f"{instance.get('Placement', {}).get('GroupName', '')}"
        for instance in instances
    }
    resources = [
        {
            "resource_id": instance.get("InstanceId", ""),
            "resource_type": "compute",
            "tenant_id": account_id,
            "topology_block_id": args.topology_block_id,
            "performance_domain": next(iter(performance_domains)) if len(performance_domains) == 1 else "mixed",
            "isolation_boundary": account_id,
        }
        for instance in instances
    ]
    if allocated["network"]:
        resources.append(
            {
                "resource_id": args.placement_group,
                "resource_type": "network",
                "tenant_id": account_id,
                "topology_block_id": args.topology_block_id,
                "performance_domain": next(iter(performance_domains)) if len(performance_domains) == 1 else "mixed",
                "isolation_boundary": account_id,
            }
        )

    allocated_as_unit = allocated == requested
    homogeneous = len(performance_domains) == 1 and all(
        instance.get("Placement", {}).get("GroupName") == args.placement_group for instance in instances
    )
    # Derive isolation from observed instance ownership rather than asserting it:
    # every launched instance must carry the tenant's OwnerAccount tag, so a
    # launch that lands outside the tenant boundary fails the contract instead
    # of being silently reported as isolated.
    owner_accounts = {_instance_owner_account(instance) for instance in instances}
    isolation_enforced = bool(instances) and owner_accounts == {account_id}
    success = allocated_as_unit and homogeneous and isolation_enforced
    return {
        "success": success,
        "platform": "aws",
        "test_name": TEST_NAME,
        "topology_block": {
            "block_id": args.topology_block_id,
            "reservation_id": reservation_id,
            "tenant_id": account_id,
            "allocated_as_unit": allocated_as_unit,
            "partial_allocation": not allocated_as_unit,
            "homogeneous": homogeneous,
            "isolation_enforced": isolation_enforced,
            "requested": requested,
            "allocated": allocated,
            "resources": resources,
        },
    }


def _terminate_instances(ec2: Any, instance_ids: list[str]) -> list[str]:
    """Terminate instances (retrying transient throttling) and wait for drain."""
    errors: list[str] = []
    # Retry the terminate call on transient throttling so a single 429 does
    # not leak instances; the waiter then blocks until they drain.
    error = _cleanup_call(
        ec2.terminate_instances,
        action="terminate_instances",
        gone_codes=INSTANCE_ALREADY_GONE_CODES,
        InstanceIds=instance_ids,
    )
    if error:
        errors.append(error)
    else:
        try:
            ec2.get_waiter("instance_terminated").wait(InstanceIds=instance_ids)
        except Exception as exc:
            errors.append(f"instance_terminated waiter: {type(exc).__name__}: {exc}")
    return errors


def _cancel_reservation(ec2: Any, reservation_id: str) -> str | None:
    """Cancel a capacity reservation, treating an already-gone id as success."""
    return _cleanup_call(
        ec2.cancel_capacity_reservation,
        action="cancel_capacity_reservation",
        gone_codes=CAPACITY_RESERVATION_ALREADY_GONE_CODES,
        CapacityReservationId=reservation_id,
    )


def _delete_placement_group(ec2: Any, placement_group: str) -> str | None:
    """Delete the cluster placement group, retrying while instances drain."""
    return _cleanup_call(
        ec2.delete_placement_group,
        action="delete_placement_group",
        gone_codes=PLACEMENT_GROUP_ALREADY_GONE_CODES,
        retry_codes=PLACEMENT_GROUP_RETRYABLE_DELETE_CODES,
        attempts=PLACEMENT_GROUP_DELETE_ATTEMPTS,
        backoff_seconds=PLACEMENT_GROUP_DELETE_BACKOFF_SECONDS,
        GroupName=placement_group,
    )


def cleanup(
    ec2: Any,
    *,
    instance_ids: list[str],
    reservation_id: str,
    placement_group: str,
) -> list[str]:
    """Best-effort cleanup for capacity reservation AWS resources."""
    errors: list[str] = []
    if instance_ids:
        errors.extend(_terminate_instances(ec2, instance_ids))
    if reservation_id:
        error = _cancel_reservation(ec2, reservation_id)
        if error:
            errors.append(error)
    if placement_group:
        error = _delete_placement_group(ec2, placement_group)
        if error:
            errors.append(error)
    return errors


def _block_tag_filters(topology_block_id: str) -> list[dict[str, Any]]:
    """EC2 tag filters that select this run's CAP04 topology block resources."""
    return [
        {"Name": "tag:CreatedBy", "Values": ["isvtest"]},
        {"Name": "tag:TestName", "Values": [TEST_NAME]},
        {"Name": "tag:TopologyBlock", "Values": [topology_block_id]},
    ]


def _tagged_active_instance_ids(ec2: Any, topology_block_id: str) -> list[str]:
    """Find non-terminated instances tagged for this topology block."""
    described = ec2.describe_instances(Filters=_block_tag_filters(topology_block_id))
    return [
        instance["InstanceId"]
        for reservation in described.get("Reservations", [])
        for instance in reservation.get("Instances", [])
        if instance.get("InstanceId") and instance.get("State", {}).get("Name") not in TERMINAL_INSTANCE_STATES
    ]


def _tagged_active_reservation_ids(ec2: Any, topology_block_id: str) -> list[str]:
    """Find active capacity reservations tagged for this topology block."""
    described = ec2.describe_capacity_reservations(Filters=_block_tag_filters(topology_block_id))
    return [
        reservation["CapacityReservationId"]
        for reservation in described.get("CapacityReservations", [])
        if reservation.get("CapacityReservationId") and reservation.get("State") not in INACTIVE_RESERVATION_STATES
    ]


def run_teardown(ec2: Any, *, topology_block_id: str, placement_group: str) -> dict[str, Any]:
    """Sweep CAP04 topology resources tagged for this block (deferred cleanup).

    Unlike the in-run ``finally`` cleanup - which deletes the exact ids this
    process created - this path runs in a standalone ``--phase teardown`` after
    AWS_CAPACITY_SKIP_DESTROY, where those ids are no longer in context. It
    therefore locates instances and the reservation by their TopologyBlock tag
    so the launched (and billable) resources are not leaked.
    """
    errors: list[str] = []
    try:
        instance_ids = _tagged_active_instance_ids(ec2, topology_block_id)
    except Exception as exc:
        errors.append(f"describe_instances for {topology_block_id}: {type(exc).__name__}: {exc}")
        instance_ids = []
    try:
        reservation_ids = _tagged_active_reservation_ids(ec2, topology_block_id)
    except Exception as exc:
        errors.append(f"describe_capacity_reservations for {topology_block_id}: {type(exc).__name__}: {exc}")
        reservation_ids = []

    if instance_ids:
        errors.extend(_terminate_instances(ec2, instance_ids))
    for reservation_id in reservation_ids:
        error = _cancel_reservation(ec2, reservation_id)
        if error:
            errors.append(error)
    if placement_group:
        error = _delete_placement_group(ec2, placement_group)
        if error:
            errors.append(error)

    result: dict[str, Any] = {"success": not errors, "platform": "aws", "test_name": TEST_NAME}
    if errors:
        result["cleanup_errors"] = errors
        result["error_type"] = "cleanup_failed"
        result["error"] = "Topology block teardown failed"
    return result


def _create_placement_group(ec2: Any, args: argparse.Namespace, account_id: str) -> None:
    """Create the cluster placement group used as the performance domain."""
    ec2.create_placement_group(
        GroupName=args.placement_group,
        Strategy="cluster",
        TagSpecifications=[
            {
                "ResourceType": "placement-group",
                "Tags": _tags(args, account_id),
            }
        ],
    )


def _placement_group_arn(ec2: Any, placement_group: str) -> str:
    """Resolve the ARN for an existing EC2 placement group."""
    response = ec2.describe_placement_groups(GroupNames=[placement_group])
    groups = response.get("PlacementGroups", [])
    if not groups or not groups[0].get("GroupArn"):
        raise RuntimeError(f"Could not resolve placement group ARN for {placement_group}")
    return groups[0]["GroupArn"]


def _create_capacity_reservation(ec2: Any, args: argparse.Namespace, account_id: str) -> str:
    """Create a targeted EC2 Capacity Reservation."""
    placement_group_arn = _placement_group_arn(ec2, args.placement_group)
    response = ec2.create_capacity_reservation(
        InstanceType=args.instance_type,
        InstancePlatform=args.instance_platform,
        AvailabilityZone=args.availability_zone,
        PlacementGroupArn=placement_group_arn,
        InstanceCount=args.requested_nodes,
        InstanceMatchCriteria="targeted",
        TagSpecifications=[
            {
                "ResourceType": "capacity-reservation",
                "Tags": _tags(args, account_id),
            }
        ],
    )
    return response["CapacityReservation"]["CapacityReservationId"]


def _create_capacity_reservation_in_available_az(
    ec2: Any,
    args: argparse.Namespace,
    account_id: str,
    candidate_azs: list[str],
) -> str:
    """Create the reservation, trying supported AZs after capacity shortages."""
    last_capacity_error: Exception | None = None
    for selected_az in candidate_azs:
        args.availability_zone = selected_az
        try:
            return _create_capacity_reservation(ec2, args, account_id)
        except Exception as error:
            if len(candidate_azs) == 1 or not _is_capacity_unavailable(error):
                raise
            last_capacity_error = error

    if last_capacity_error is not None:
        raise last_capacity_error
    raise RuntimeError(f"No AWS availability zones offer {args.instance_type}")


def _launch_instances(
    ec2: Any,
    args: argparse.Namespace,
    *,
    account_id: str,
    reservation_id: str,
    launched_instance_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Launch instances bound to the reservation and placement group."""
    ami_id = args.ami_id or get_ubuntu_ami(ec2, args.instance_type)
    if not ami_id:
        raise RuntimeError(f"Could not find an AMI for {args.instance_type}")

    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=args.instance_type,
        MinCount=args.requested_nodes,
        MaxCount=args.requested_nodes,
        CapacityReservationSpecification={
            "CapacityReservationTarget": {
                "CapacityReservationId": reservation_id,
            }
        },
        Placement={
            "AvailabilityZone": args.availability_zone,
            "GroupName": args.placement_group,
        },
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": _tags(args, account_id),
            }
        ],
    )
    instance_ids = [instance["InstanceId"] for instance in response.get("Instances", [])]
    if launched_instance_ids is not None:
        launched_instance_ids.extend(instance_ids)
    if not instance_ids:
        return []

    ec2.get_waiter("instance_running").wait(InstanceIds=instance_ids)
    described = ec2.describe_instances(InstanceIds=instance_ids)
    return [
        instance
        for reservation in described.get("Reservations", [])
        for instance in reservation.get("Instances", [])
        if instance.get("InstanceId") in instance_ids
    ]


def main() -> int:
    """Run the AWS capacity reservation topology block atomic allocation check."""
    parser = argparse.ArgumentParser(description="Validate AWS atomic topology block allocation")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--tenant-id", default="", help="AWS account ID; resolved from STS when empty")
    parser.add_argument("--topology-block-id", required=True, help="Provider-neutral topology block ID")
    parser.add_argument("--instance-type", required=True, help="EC2 instance type")
    parser.add_argument("--instance-platform", default="Linux/UNIX", help="EC2 Capacity Reservation platform")
    parser.add_argument("--availability-zone", required=True, help="Availability zone for the reserved block")
    parser.add_argument("--placement-group", required=True, help="Cluster placement group name")
    parser.add_argument("--requested-nodes", type=_positive_int, required=True, help="Requested compute nodes")
    parser.add_argument(
        "--requested-network", type=_non_negative_int, required=True, help="Requested network resources"
    )
    parser.add_argument(
        "--requested-storage", type=_non_negative_int, required=True, help="Requested storage resources"
    )
    parser.add_argument("--ami-id", default="", help="Optional AMI ID; auto-detected when omitted")
    parser.add_argument("--skip-destroy", action="store_true", help="Leave AWS resources in place for debugging")
    parser.add_argument(
        "--teardown",
        action="store_true",
        help="Sweep resources tagged for this topology block instead of allocating",
    )
    args = parser.parse_args()

    if args.teardown:
        if args.skip_destroy:
            result = {
                "success": True,
                "platform": "aws",
                "test_name": TEST_NAME,
                "skipped": "AWS_CAPACITY_SKIP_DESTROY set",
            }
        else:
            ec2 = boto3.client("ec2", region_name=args.region)
            result = run_teardown(ec2, topology_block_id=args.topology_block_id, placement_group=args.placement_group)
        print(json.dumps(result, indent=2))
        return 0 if result.get("success") is True else 1

    ec2: Any | None = None
    reservation_id = ""
    placement_group = ""
    instance_ids: list[str] = []
    result: dict[str, Any]

    try:
        _validate_supported_resource_counts(args)
        ec2 = boto3.client("ec2", region_name=args.region)
        sts = boto3.client("sts", region_name=args.region)
        account_id = args.tenant_id or sts.get_caller_identity()["Account"]
        candidate_azs = candidate_availability_zones(ec2, args.availability_zone, args.instance_type)
        _create_placement_group(ec2, args, account_id)
        placement_group = args.placement_group
        reservation_id = _create_capacity_reservation_in_available_az(ec2, args, account_id, candidate_azs)
        instances = _launch_instances(
            ec2,
            args,
            account_id=account_id,
            reservation_id=reservation_id,
            launched_instance_ids=instance_ids,
        )
        result = build_contract(args, account_id=account_id, reservation_id=reservation_id, instances=instances)
    except Exception as exc:
        error_type, error_message = _classify_error(exc)
        result = _failure(error_message, error_type=error_type)
    finally:
        if not args.skip_destroy and ec2 is not None:
            cleanup_errors = cleanup(
                ec2,
                instance_ids=instance_ids,
                reservation_id=reservation_id,
                placement_group=placement_group,
            )
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors
                if result.get("success") is True:
                    result["success"] = False
                    result["error_type"] = "cleanup_failed"
                    result["error"] = "Topology block cleanup failed"

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") is True else 1


if __name__ == "__main__":
    sys.exit(main())
