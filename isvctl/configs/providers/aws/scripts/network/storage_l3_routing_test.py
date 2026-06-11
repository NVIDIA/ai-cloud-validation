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

"""Test all-to-all L3 routing between storage hosts (SDN08-01).

Proves that storage hosts spread across multiple subnets of one VPC reach every
other host over L3 (full mesh), with intra-VPC traffic routed on the VPC local
route rather than through a gateway.

Self-contained: creates a VPC with two subnets in distinct AZs and one security
group permitting intra-VPC traffic, launches N hosts spread across the subnets,
probes the full mesh by private IP over SSM, confirms cross-subnet pairs are
reachable, and verifies every cross-subnet route resolves through the VPC local
route table entry rather than a gateway, then cleans everything up.

Usage:
    python storage_l3_routing_test.py --region us-west-2 --cidr 10.86.0.0/16 --hosts 3

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "storage_l3_routing",
    "tests": {
        "distinct_subnets":     {"passed": true, "subnet_count": 2},
        "all_to_all_reachable": {"passed": true, "pairs_tested": 6, "pairs_reachable": 6},
        "cross_subnet_routing": {"passed": true, "pairs_tested": 4, "pairs_reachable": 4},
        "no_gateway_hop":       {"passed": true, "pairs_tested": 4, "pairs_direct": 4}
    }
}
"""

import argparse
import ipaddress
import itertools
import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import (
    create_ssm_instance_profile,
    delete_ssm_instance_profile,
    get_amazon_linux_ami,
    run_ssm_command,
    wait_ssm_ready_all,
)
from common.errors import handle_aws_errors
from common.vpc import cleanup_vpc_resources, create_test_vpc

SSM_ENDPOINT_SERVICES = ("ssm", "ssmmessages", "ec2messages")
ROUTE_TARGET_KEYS = (
    "GatewayId",
    "NatGatewayId",
    "TransitGatewayId",
    "VpcPeeringConnectionId",
    "NetworkInterfaceId",
    "EgressOnlyInternetGatewayId",
    "LocalGatewayId",
    "CarrierGatewayId",
    "CoreNetworkArn",
)


def create_subnets(ec2: Any, vpc_id: str, cidr: str, suffix: str, created_ids: list[str]) -> list[dict[str, Any]]:
    """Create two /24 subnets in distinct AZs within the VPC CIDR.

    Each subnet ID is appended to ``created_ids`` as soon as it is created so the
    caller's cleanup path can reclaim partially-created resources on failure.
    """
    try:
        network = ipaddress.ip_network(cidr)
    except ValueError as exc:
        raise RuntimeError(f"Invalid CIDR {cidr!r}: {exc}") from exc
    if network.prefixlen > 24:
        raise RuntimeError(f"CIDR {cidr} cannot provide two /24 subnets")
    candidates = list(network.subnets(new_prefix=24))
    if len(candidates) < 2:
        raise RuntimeError(f"CIDR {cidr} cannot provide two /24 subnets")
    azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
    zone_names = [z["ZoneName"] for z in azs["AvailabilityZones"]]
    if len(zone_names) < 2:
        raise RuntimeError("Need at least two availability zones for cross-subnet routing")

    subnets = []
    for i in range(2):
        resp = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=str(candidates[i]),
            AvailabilityZone=zone_names[i],
        )
        subnet_id = resp["Subnet"]["SubnetId"]
        created_ids.append(subnet_id)
        ec2.create_tags(
            Resources=[subnet_id],
            Tags=[
                {"Key": "Name", "Value": f"isv-storage-l3-{i}-{suffix}"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )
        subnets.append({"subnet_id": subnet_id, "cidr": str(candidates[i]), "az": zone_names[i]})
    return subnets


def create_intra_vpc_sg(ec2: Any, vpc_id: str, vpc_cidr: str, suffix: str) -> str:
    """Create a security group permitting all traffic within the VPC CIDR."""
    sg = ec2.create_security_group(
        GroupName=f"isv-storage-l3-{suffix}",
        Description="Intra-VPC all-to-all storage routing test",
        VpcId=vpc_id,
    )
    sg_id = sg["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": vpc_cidr}]}],
    )
    return sg_id


def create_ssm_vpc_endpoints(
    ec2: Any,
    vpc_id: str,
    subnet_ids: list[str],
    sg_id: str,
    region: str,
    suffix: str,
) -> list[str]:
    """Create private SSM interface endpoints for instances in isolated subnets."""
    endpoint_ids: list[str] = []
    try:
        for service in SSM_ENDPOINT_SERVICES:
            endpoint = ec2.create_vpc_endpoint(
                VpcId=vpc_id,
                ServiceName=f"com.amazonaws.{region}.{service}",
                VpcEndpointType="Interface",
                SubnetIds=subnet_ids,
                SecurityGroupIds=[sg_id],
                PrivateDnsEnabled=True,
                TagSpecifications=[
                    {
                        "ResourceType": "vpc-endpoint",
                        "Tags": [
                            {"Key": "Name", "Value": f"isv-storage-l3-{service}-{suffix}"},
                            {"Key": "CreatedBy", "Value": "isvtest"},
                        ],
                    }
                ],
            )
            endpoint_ids.append(endpoint["VpcEndpoint"]["VpcEndpointId"])
        _wait_for_vpc_endpoints_available(ec2, endpoint_ids)
    except Exception:
        if endpoint_ids:
            try:
                delete_ssm_vpc_endpoints(ec2, endpoint_ids)
            except Exception:
                pass
        raise
    return endpoint_ids


def delete_ssm_vpc_endpoints(ec2: Any, endpoint_ids: list[str]) -> None:
    """Delete private SSM interface endpoints and wait for endpoint ENIs to detach."""
    if not endpoint_ids:
        return
    delete_result = ec2.delete_vpc_endpoints(VpcEndpointIds=endpoint_ids)
    unsuccessful = delete_result.get("Unsuccessful", [])
    if unsuccessful:
        raise RuntimeError(f"delete_vpc_endpoints reported unsuccessful entries: {unsuccessful}")
    for endpoint_id in endpoint_ids:
        _wait_for_vpc_endpoint_deletion(ec2, endpoint_id)


def _wait_for_vpc_endpoints_available(
    ec2: Any,
    endpoint_ids: list[str],
    attempts: int = 60,
    delay: float = 2.0,
) -> None:
    """Poll VPC endpoints until all are available."""
    pending = set(endpoint_ids)
    for _ in range(attempts):
        resp = ec2.describe_vpc_endpoints(VpcEndpointIds=list(pending))
        endpoints = resp.get("VpcEndpoints", [])
        pending = {ep["VpcEndpointId"] for ep in endpoints if ep.get("State") != "available"}
        if not pending:
            return
        time.sleep(delay)
    raise TimeoutError(f"Timed out waiting for VPC endpoints to become available: {', '.join(sorted(pending))}")


def _wait_for_vpc_endpoint_deletion(
    ec2: Any,
    endpoint_id: str,
    attempts: int = 90,
    delay: float = 2.0,
) -> None:
    """Poll until a VPC endpoint is gone so dependent subnets and SGs can be deleted."""
    for _ in range(attempts):
        try:
            resp = ec2.describe_vpc_endpoints(VpcEndpointIds=[endpoint_id])
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "InvalidVpcEndpointId.NotFound":
                return
            raise
        endpoints = resp.get("VpcEndpoints", [])
        if not endpoints or endpoints[0].get("State") == "deleted":
            return
        time.sleep(delay)
    raise TimeoutError(f"Timed out waiting for VPC endpoint {endpoint_id} deletion")


def launch_hosts(
    ec2: Any,
    ami: str,
    subnets: list[dict[str, Any]],
    sg_id: str,
    profile: str,
    count: int,
    created_ids: list[str],
) -> list[dict[str, Any]]:
    """Launch `count` hosts round-robin across the subnets.

    Each instance ID is appended to ``created_ids`` as soon as it is launched so
    the caller's cleanup path can terminate instances even if a later step (e.g.
    the running-state waiter) raises before all hosts are up.
    """
    hosts = []
    for i in range(count):
        subnet = subnets[i % len(subnets)]
        resp = ec2.run_instances(
            ImageId=ami,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet["subnet_id"],
            SecurityGroupIds=[sg_id],
            IamInstanceProfile={"Name": profile},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": f"isv-storage-host-{i}"},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        instance_id = resp["Instances"][0]["InstanceId"]
        created_ids.append(instance_id)
        hosts.append({"instance_id": instance_id, "subnet_id": subnet["subnet_id"]})

    instance_ids = [h["instance_id"] for h in hosts]
    ec2.get_waiter("instance_running").wait(InstanceIds=instance_ids)

    described = ec2.describe_instances(InstanceIds=instance_ids)
    ip_by_id = {
        inst["InstanceId"]: inst.get("PrivateIpAddress")
        for res in described["Reservations"]
        for inst in res["Instances"]
    }
    for host in hosts:
        host["private_ip"] = ip_by_id.get(host["instance_id"])
    return hosts


def check_cross_subnet_local_routes(
    ec2: Any,
    vpc_id: str,
    cross_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Verify each cross-subnet destination selects the source subnet's local VPC route."""
    result: dict[str, Any] = {
        "passed": False,
        "pairs_tested": len(cross_pairs),
        "pairs_direct": 0,
    }
    if not cross_pairs:
        result["error"] = "No cross-subnet host pairs to verify"
        return result

    route_tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("RouteTables", [])
    failures: list[str] = []
    for src, dst in cross_pairs:
        route_table = _route_table_for_subnet(route_tables, src["subnet_id"])
        if route_table is None:
            failures.append(f"no route table for source subnet {src['subnet_id']}")
            continue

        route = _best_route_for_ip(route_table, dst["private_ip"])
        if route is None:
            failures.append(f"no route from {src['subnet_id']} to {dst['private_ip']}")
            continue

        if route.get("GatewayId") == "local":
            result["pairs_direct"] += 1
        else:
            failures.append(
                f"{src['subnet_id']} to {dst['private_ip']} uses {_route_target(route)} "
                f"in {route_table.get('RouteTableId', 'unknown route table')}"
            )

    result["passed"] = result["pairs_direct"] == len(cross_pairs)
    if failures:
        result["error"] = f"{len(failures)} cross-subnet route(s) did not use the VPC local route: {failures[0]}"
    return result


def _route_table_for_subnet(route_tables: list[dict[str, Any]], subnet_id: str) -> dict[str, Any] | None:
    """Return the explicit subnet route table, or the VPC main route table."""
    main_table = None
    for route_table in route_tables:
        for assoc in route_table.get("Associations", []):
            if assoc.get("SubnetId") == subnet_id:
                return route_table
            if assoc.get("Main"):
                main_table = route_table
    return main_table


def _best_route_for_ip(route_table: dict[str, Any], private_ip: str) -> dict[str, Any] | None:
    """Return the active IPv4 route selected by longest-prefix match."""
    address = ipaddress.ip_address(private_ip)
    best: tuple[int, dict[str, Any]] | None = None
    for route in route_table.get("Routes", []):
        if route.get("State", "active") != "active":
            continue
        cidr = route.get("DestinationCidrBlock")
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if address.version != network.version or address not in network:
            continue
        candidate = (network.prefixlen, route)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def _route_target(route: dict[str, Any]) -> str:
    """Return a concise route target description for failure diagnostics."""
    for key in ROUTE_TARGET_KEYS:
        if route.get(key):
            return f"{key}={route[key]}"
    return "an unknown route target"


@handle_aws_errors
def main() -> int:
    """Run the AWS storage L3 routing probe and emit the validation JSON result.

    Returns 0 when every validation and cleanup step succeeds, otherwise 1. This
    function parses CLI arguments, makes AWS API calls, logs errors through the
    JSON result payload, and may exit through argparse for invalid input.
    """
    parser = argparse.ArgumentParser(description="Test all-to-all storage L3 routing")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.86.0.0/16", help="Private network CIDR")
    parser.add_argument("--hosts", type=int, default=3, help="Number of storage hosts (>=3)")
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()
    if args.hosts < 3:
        parser.error("--hosts must be >= 3")
    host_count = args.hosts

    ec2 = boto3.client("ec2", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)
    suffix = str(uuid.uuid4())[:8]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "storage_l3_routing",
        "tests": {},
    }

    vpc_id = None
    subnet_ids: list[str] = []
    sg_id = None
    role_name = None
    profile_name = None
    instance_ids: list[str] = []
    ssm_endpoint_ids: list[str] = []

    try:
        ami = get_amazon_linux_ami(ec2)
        if not ami:
            raise RuntimeError("Could not find Amazon Linux AMI")

        vpc = create_test_vpc(ec2, args.cidr, f"isv-storage-l3-{suffix}", enable_dns=True)
        vpc_id = vpc.get("vpc_id")
        if not vpc["passed"]:
            # Raise instead of printing-and-returning so the finally block runs
            # cleanup and the single trailing print reflects its outcome.
            raise RuntimeError(vpc.get("error", "VPC creation failed"))

        subnets = create_subnets(ec2, vpc_id, args.cidr, suffix, subnet_ids)
        sg_id = create_intra_vpc_sg(ec2, vpc_id, args.cidr, suffix)
        ssm_endpoint_ids = create_ssm_vpc_endpoints(ec2, vpc_id, subnet_ids, sg_id, args.region, suffix)

        role_name, profile_name = create_ssm_instance_profile(
            iam, description="Temporary role for storage L3 routing test"
        )
        hosts = launch_hosts(ec2, ami, subnets, sg_id, profile_name, host_count, instance_ids)

        # distinct_subnets: hosts must land in >= 2 subnets
        landed = {h["subnet_id"] for h in hosts}
        result["tests"]["distinct_subnets"] = {
            "passed": len(landed) >= 2,
            "subnet_count": len(landed),
        }

        # Wait for the SSM agent on every host to register (longer with a fresh
        # IAM profile). A single shared deadline across all hosts keeps the total
        # wait bounded by one timeout rather than one timeout per host.
        not_ready = wait_ssm_ready_all(ssm, [h["instance_id"] for h in hosts])
        if not_ready:
            raise RuntimeError(f"SSM agent did not come online on: {', '.join(not_ready)}")

        # all_to_all_reachable: ping every directed host pair by private IP.
        pairs = list(itertools.permutations(hosts, 2))
        reachable = 0
        cross_subnet_reachable = 0
        cross_pairs = [(a, b) for a, b in pairs if a["subnet_id"] != b["subnet_id"]]
        cross_subnet_total = len(cross_pairs)
        for a, b in pairs:
            ok, _ = run_ssm_command(ssm, a["instance_id"], f"ping -c 3 -W 2 {b['private_ip']}")
            if ok:
                reachable += 1
                if a["subnet_id"] != b["subnet_id"]:
                    cross_subnet_reachable += 1
        result["tests"]["all_to_all_reachable"] = {
            "passed": reachable == len(pairs),
            "pairs_tested": len(pairs),
            "pairs_reachable": reachable,
        }
        result["tests"]["cross_subnet_routing"] = {
            "passed": cross_subnet_total > 0 and cross_subnet_reachable == cross_subnet_total,
            "pairs_tested": cross_subnet_total,
            "pairs_reachable": cross_subnet_reachable,
        }

        # no_gateway_hop: AWS guests normally route cross-subnet traffic via the
        # subnet router, so validate the effective VPC route-table entry instead.
        result["tests"]["no_gateway_hop"] = check_cross_subnet_local_routes(ec2, vpc_id, cross_pairs)

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:  # Surface as JSON error for the validation layer.
        result["error"] = str(e)
    finally:
        if not args.skip_cleanup:
            cleanup_errors: list[str] = []
            try:
                if instance_ids:
                    ec2.terminate_instances(InstanceIds=instance_ids)
                    ec2.get_waiter("instance_terminated").wait(InstanceIds=instance_ids)
            except Exception as e:
                cleanup_errors.append(f"instance cleanup failed: {e}")
            try:
                if ssm_endpoint_ids:
                    delete_ssm_vpc_endpoints(ec2, ssm_endpoint_ids)
            except Exception as e:
                cleanup_errors.append(f"endpoint cleanup failed: {e}")
            try:
                if vpc_id:
                    cleanup_vpc_resources(
                        ec2,
                        vpc_id,
                        subnet_ids=subnet_ids,
                        sg_ids=[sg_id] if sg_id else None,
                    )
            except Exception as e:
                cleanup_errors.append(f"vpc cleanup failed: {e}")
            try:
                if role_name and profile_name:
                    delete_ssm_instance_profile(iam, role_name, profile_name)
            except Exception as e:
                cleanup_errors.append(f"iam cleanup failed: {e}")
            result["cleanup"] = not cleanup_errors
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors
                cleanup_error = f"Cleanup failed: {'; '.join(cleanup_errors)}"
                result["error"] = f"{result['error']}; {cleanup_error}" if result.get("error") else cleanup_error
                result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
