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

"""Verify AWS capacity can be grouped and pinned to one account.

The script creates an EC2 Capacity Reservation with targeted matching and tags
it into an AWS Resource Group. It emits the provider-neutral capacity
reservation JSON contract consumed by ``CapacityReservationGroupingCheck``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import candidate_availability_zones
from common.errors import classify_aws_error, delete_with_retry, handle_aws_errors

CAP_GROUP_TAG = "isv-capacity-group"
CAP_PURPOSE_TAG = "CAP"
CAPACITY_SHORTAGE_CODES = frozenset({"InsufficientInstanceCapacity"})


def _tag_specifications(group_name: str, account_id: str) -> list[dict[str, Any]]:
    """Build capacity reservation tags used for stable resource grouping."""
    return [
        {
            "ResourceType": "capacity-reservation",
            "Tags": [
                {"Key": "Name", "Value": group_name},
                {"Key": "CreatedBy", "Value": "isvtest"},
                {"Key": CAP_PURPOSE_TAG, "Value": "capacity-reservation-grouping"},
                {"Key": CAP_GROUP_TAG, "Value": group_name},
                {"Key": "isv-account-id", "Value": account_id},
            ],
        }
    ]


def _resource_group_query(group_name: str) -> dict[str, str]:
    """Build a Resource Groups tag query for CAP-tagged reservations."""
    return {
        "Type": "TAG_FILTERS_1_0",
        "Query": json.dumps(
            {
                "ResourceTypeFilters": ["AWS::EC2::CapacityReservation"],
                "TagFilters": [
                    {"Key": "CreatedBy", "Values": ["isvtest"]},
                    {"Key": CAP_GROUP_TAG, "Values": [group_name]},
                ],
            }
        ),
    }


def _is_capacity_shortage(error: Exception) -> bool:
    """Return whether an AWS error indicates no allocatable capacity in an AZ."""
    if not isinstance(error, ClientError):
        return False
    return str(error.response.get("Error", {}).get("Code", "")) in CAPACITY_SHORTAGE_CODES


def _ensure_resource_group(resource_groups: Any, group_name: str) -> tuple[str, bool]:
    """Create or resolve the Resource Group used to group CAP resources.

    Returns the group ARN and whether this call created it. A pre-existing group
    is resolved rather than created, so callers can avoid deleting a group they
    did not create during cleanup.
    """
    try:
        response = resource_groups.create_group(
            Name=group_name,
            Description="ISV capacity reservation grouping validation",
            ResourceQuery=_resource_group_query(group_name),
            Tags={"CreatedBy": "isvtest", "purpose": "capacity-reservation-grouping"},
        )
        return str(response["Group"]["GroupArn"]), True
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        message = error.response.get("Error", {}).get("Message", "")
        if code == "BadRequestException" and "exist" in message.lower():
            response = resource_groups.get_group(GroupName=group_name)
            return str(response["Group"]["GroupArn"]), False
        raise


def _create_capacity_reservation(
    ec2: Any,
    *,
    instance_type: str,
    availability_zone: str,
    reservation_count: int,
    group_name: str,
    account_id: str,
) -> dict[str, Any]:
    """Create a targeted EC2 Capacity Reservation for the requested instance type."""
    response = ec2.create_capacity_reservation(
        InstanceType=instance_type,
        InstancePlatform="Linux/UNIX",
        AvailabilityZone=availability_zone,
        InstanceCount=reservation_count,
        InstanceMatchCriteria="targeted",
        TagSpecifications=_tag_specifications(group_name, account_id),
    )
    return dict(response["CapacityReservation"])


def _describe_capacity_reservation(ec2: Any, reservation_id: str) -> dict[str, Any]:
    """Describe an existing capacity reservation."""
    response = ec2.describe_capacity_reservations(CapacityReservationIds=[reservation_id])
    reservations = response.get("CapacityReservations", [])
    if not reservations:
        raise RuntimeError(f"Capacity reservation {reservation_id} was not found")
    return dict(reservations[0])


def _contract_from_reservation(reservation: dict[str, Any], account_id: str) -> dict[str, Any]:
    """Build the provider-neutral capacity reservation grouping contract."""
    reservation_id = str(reservation.get("CapacityReservationId") or "")
    owner_id = str(reservation.get("OwnerId") or "")
    instance_type = str(reservation.get("InstanceType") or "")
    pinned = reservation.get("InstanceMatchCriteria") == "targeted"

    resources: list[dict[str, Any]] = []
    if reservation_id:
        resources.append(
            {
                "resource_id": reservation_id,
                "resource_type": "compute",
                "account_id": owner_id,
                "pinned": pinned,
            }
        )
    if instance_type:
        resources.append(
            {
                "resource_id": instance_type,
                "resource_type": "instance_type",
                "account_id": owner_id,
                "pinned": pinned,
            }
        )

    isolation_enforced = bool(owner_id == account_id and resources and pinned)
    result: dict[str, Any] = {
        "success": False,
        "platform": "aws",
        "reservation_id": reservation_id,
        "account_id": account_id,
        "resources": resources,
        "pinned": pinned,
        "isolation_enforced": isolation_enforced,
    }

    if not reservation_id:
        result["error"] = "AWS response did not include a capacity reservation id"
    elif owner_id != account_id:
        result["error"] = f"Capacity reservation owner account {owner_id!r} does not match caller {account_id!r}"
    elif not pinned:
        result["error"] = "Capacity reservation must use targeted matching"
    else:
        result["success"] = True
    return result


_INACTIVE_RESERVATION_STATES = frozenset({"cancelled", "expired", "failed"})


def run(
    *,
    ec2: Any,
    sts: Any,
    resource_groups: Any,
    region: str,
    instance_type: str,
    availability_zone: str,
    resource_group_name: str,
    reservation_count: int,
    reservation_id: str = "",
) -> dict[str, Any]:
    """Create/inspect the capacity reservation and return the grouping contract.

    Resource cleanup is handled by :func:`run_teardown` in the teardown phase,
    not here, so a failed validation does not also trigger a destroy. The output
    carries ``resource_group_created`` so the teardown step only deletes a group
    this run created (a pre-existing group is preserved).
    """
    group_created = False
    reservation_created = False
    created_reservation_id = ""
    try:
        account_id = str(sts.get_caller_identity()["Account"])
        candidate_azs = candidate_availability_zones(ec2, availability_zone, instance_type)
        _, group_created = _ensure_resource_group(resource_groups, resource_group_name)

        if not reservation_id:
            last_capacity_error: Exception | None = None
            for selected_az in candidate_azs:
                try:
                    created = _create_capacity_reservation(
                        ec2,
                        instance_type=instance_type,
                        availability_zone=selected_az,
                        reservation_count=reservation_count,
                        group_name=resource_group_name,
                        account_id=account_id,
                    )
                    break
                except Exception as error:
                    if availability_zone.strip() or not _is_capacity_shortage(error):
                        raise
                    last_capacity_error = error
            else:
                if last_capacity_error is not None:
                    raise last_capacity_error
                raise RuntimeError(f"No AWS availability zones offer {instance_type}")
            reservation_id = str(created.get("CapacityReservationId") or "")
            if not reservation_id:
                raise RuntimeError("AWS response did not include a capacity reservation id")
            reservation_created = True
            created_reservation_id = reservation_id

        # Read AWS's persisted reservation state rather than the create echo, so
        # the grouping/pinning contract reflects what AWS actually stored.
        reservation = _describe_capacity_reservation(ec2, reservation_id)
        result = _contract_from_reservation(reservation, account_id)
    except Exception as error:
        error_type, error_message = classify_aws_error(error)
        result = {
            "success": False,
            "platform": "aws",
            "error_type": error_type,
            "error": error_message,
        }

    # Surface teardown wiring on every path so the teardown step can clean up
    # whatever was created, even after a validation failure.
    result["resource_group_created"] = group_created
    result["reservation_created"] = reservation_created
    result["created_reservation_id"] = created_reservation_id
    return result


def run_teardown(
    *,
    ec2: Any,
    resource_groups: Any,
    resource_group_name: str,
    delete_group: bool,
    reservation_created: bool = False,
    created_reservation_id: str = "",
) -> dict[str, Any]:
    """Cancel reservations tagged for this group and optionally delete the group.

    Reservations are located by their CAP group tag. When ``created_reservation_id``
    identifies the reservation this run created, cancellation is narrowed to it -
    and a stale id without ``reservation_created`` is not enough to authorize it -
    so an in-run teardown never cancels a reservation a concurrent run created
    under the same group tag. When no created-reservation id is supplied (e.g. a
    standalone ``--phase teardown`` after AWS_CAPACITY_SKIP_DESTROY, where the
    setup step output is not in context), teardown falls back to cancelling every
    reservation tagged for this group so deferred cleanup does not leak them.

    The group is only deleted when ``delete_group`` is set (i.e. this run created
    it), preserving a group that pre-existed.
    """
    errors: list[str] = []
    try:
        response = ec2.describe_capacity_reservations(
            Filters=[{"Name": f"tag:{CAP_GROUP_TAG}", "Values": [resource_group_name]}]
        )
    except Exception as error:
        errors.append(f"describe capacity reservations for {resource_group_name}: {error}")
        response = {}

    for reservation in response.get("CapacityReservations", []):
        reservation_id = str(reservation.get("CapacityReservationId") or "")
        state = str(reservation.get("State") or "")
        if not reservation_id or state in _INACTIVE_RESERVATION_STATES:
            continue
        # Narrow to the reservation this run created when its id is known;
        # otherwise (standalone teardown) sweep all reservations carrying the
        # group tag rather than leaking them.
        if created_reservation_id and (not reservation_created or reservation_id != created_reservation_id):
            continue
        if not delete_with_retry(
            ec2.cancel_capacity_reservation,
            CapacityReservationId=reservation_id,
            resource_desc=f"capacity reservation {reservation_id}",
        ):
            errors.append(f"cancel capacity reservation {reservation_id}")

    if delete_group and not delete_with_retry(
        resource_groups.delete_group,
        GroupName=resource_group_name,
        resource_desc=f"resource group {resource_group_name}",
    ):
        errors.append(f"delete resource group {resource_group_name}")

    result: dict[str, Any] = {"success": not errors, "platform": "aws"}
    if errors:
        result["cleanup_errors"] = errors
        result["error"] = "Capacity reservation teardown failed"
    return result


@handle_aws_errors
def main() -> int:
    """Parse arguments, run the AWS capacity check, and print JSON."""
    parser = argparse.ArgumentParser(description="Validate AWS capacity reservation grouping")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--instance-type", default="g4dn.metal")
    parser.add_argument("--availability-zone", default="")
    parser.add_argument("--resource-group-name", default="isv-capacity-cap04")
    parser.add_argument("--reservation-count", type=int, default=1)
    parser.add_argument(
        "--reservation-id", default="", help="Inspect an existing capacity reservation instead of creating one"
    )
    parser.add_argument("--teardown", action="store_true", help="Cancel/delete resources from a prior run")
    parser.add_argument("--delete-group", action="store_true", help="Delete the Resource Group during teardown")
    parser.add_argument(
        "--reservation-created", action="store_true", help="Allow cleanup of the setup-created reservation"
    )
    parser.add_argument("--created-reservation-id", default="", help="Only cancel the reservation created by setup")
    parser.add_argument("--skip-destroy", action="store_true", help="No-op teardown; preserve created resources")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    resource_groups = boto3.client("resource-groups", region_name=args.region)

    if args.teardown:
        if args.skip_destroy:
            result: dict[str, Any] = {"success": True, "platform": "aws", "skipped": "AWS_CAPACITY_SKIP_DESTROY set"}
        else:
            result = run_teardown(
                ec2=ec2,
                resource_groups=resource_groups,
                resource_group_name=args.resource_group_name,
                delete_group=args.delete_group,
                reservation_created=args.reservation_created,
                created_reservation_id=args.created_reservation_id,
            )
    else:
        result = run(
            ec2=ec2,
            sts=boto3.client("sts", region_name=args.region),
            resource_groups=resource_groups,
            region=args.region,
            instance_type=args.instance_type,
            availability_zone=args.availability_zone,
            resource_group_name=args.resource_group_name,
            reservation_count=args.reservation_count,
            reservation_id=args.reservation_id,
        )
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
