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

"""Probe default-deny across a tenant-to-provider segment boundary (SEC30-01).

Builds one VPC with a tenant subnet and a provider subnet. The subnets share
the VPC's local route, so any blocked probe is blocked by policy, not by a
missing route. The provider target's security group allows exactly one flow
(TCP 22 from the tenant subnet, answered by sshd) as the positive control and
nothing else. A tenant source then probes, over SSM, the positive control
first and a set of prohibited flows after it.

A security group drops denied packets silently, so a denied TCP probe times
out, while a port the group admits but nothing listens on is refused by the
host. That distinction is what lets ``timeout`` stand for "denied by policy".

Usage:
    python segment_boundary_test.py --region us-west-2 --cidr 10.86.0.0/16
    python segment_boundary_test.py --region us-west-2 --also-allow-port 8080  # expected to fail SEC30-01

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "segment_boundary",
    "positive_control": {"protocol": "tcp", "port": 22, "result": "connected"},
    "prohibited_flows": [
        {"protocol": "icmp", "result": "timeout"},
        {"protocol": "tcp", "port": 443, "result": "timeout"},
        {"protocol": "tcp", "port": 8080, "result": "timeout"}
    ]
}
"""

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from common.ec2 import (
    create_ssm_instance_profile,
    delete_ssm_instance_profile,
    get_amazon_linux_ami,
    run_ssm_command,
    wait_ssm_ready,
)
from common.errors import TRANSIENT_AWS_CODES, delete_with_retry, handle_aws_errors
from common.vpc import create_test_vpc, delete_vpc

POSITIVE_CONTROL = {"protocol": "tcp", "port": 22}
PROHIBITED_FLOWS = [{"protocol": "icmp"}, {"protocol": "tcp", "port": 443}, {"protocol": "tcp", "port": 8080}]
PROBE_TIMEOUT_SECONDS = 5
CONTROL_ATTEMPTS = 3


def _tag_spec(resource_type: str, name: str) -> list[dict[str, Any]]:
    """Return a TagSpecifications entry marking the resource as isvtest-owned."""
    tags = [{"Key": "Name", "Value": name}, {"Key": "CreatedBy", "Value": "isvtest"}]
    return [{"ResourceType": resource_type, "Tags": tags}]


def probe(ssm: Any, source_id: str, target_ip: str, flow: dict[str, Any]) -> str:
    """Probe ``flow`` from the source to the target; return connected/refused/timeout/error."""
    if flow["protocol"] == "icmp":
        command = (
            f"ping -c 3 -W 2 {target_ip} >/dev/null 2>&1; rc=$?; "
            "if [ $rc -eq 0 ]; then echo connected; elif [ $rc -eq 1 ]; then echo timeout; else echo error; fi"
        )
    else:
        command = (
            f"timeout {PROBE_TIMEOUT_SECONDS} bash -c '</dev/tcp/{target_ip}/{flow['port']}' 2>/dev/null; rc=$?; "
            "if [ $rc -eq 0 ]; then echo connected; elif [ $rc -eq 124 ]; then echo timeout; else echo refused; fi"
        )
    ok, output = run_ssm_command(ssm, source_id, command)
    result = output.strip()
    return result if ok and result in ("connected", "refused", "timeout") else "error"


def cleanup(ec2: Any, iam: Any, resources: dict[str, Any]) -> None:
    """Best-effort teardown of everything the probe created, in dependency order."""
    instance_ids = resources["instance_ids"]
    if instance_ids:
        delete_with_retry(ec2.terminate_instances, InstanceIds=instance_ids, resource_desc="probe instances")
        try:
            ec2.get_waiter("instance_terminated").wait(InstanceIds=instance_ids)
        except Exception as e:
            print(f"Warning: waiting for instance termination failed: {e}", file=sys.stderr)

    # ENIs of just-terminated instances can hold a security group for a few seconds.
    for sg_id in resources["sg_ids"]:
        delete_with_retry(
            ec2.delete_security_group,
            GroupId=sg_id,
            resource_desc=f"security group {sg_id}",
            attempts=6,
            backoff_seconds=5.0,
            transient_codes=TRANSIENT_AWS_CODES | {"DependencyViolation"},
        )
    for subnet_id in resources["subnet_ids"]:
        delete_with_retry(ec2.delete_subnet, SubnetId=subnet_id, resource_desc=f"subnet {subnet_id}")
    if resources["rtb_id"]:
        delete_with_retry(ec2.delete_route_table, RouteTableId=resources["rtb_id"], resource_desc="route table")
    vpc_id = resources["vpc_id"]
    if resources["igw_id"]:
        igw_id = resources["igw_id"]
        delete_with_retry(ec2.detach_internet_gateway, InternetGatewayId=igw_id, VpcId=vpc_id, resource_desc="IGW")
        delete_with_retry(ec2.delete_internet_gateway, InternetGatewayId=igw_id, resource_desc="IGW")
    if vpc_id:
        delete_vpc(ec2, vpc_id)
    if resources["role_name"]:
        delete_ssm_instance_profile(iam, resources["role_name"], resources["profile_name"])


def build_boundary(
    ec2: Any, iam: Any, cidr: str, suffix: str, resources: dict[str, Any], also_allow_port: int | None
) -> tuple[str, str]:
    """Create the tenant/provider topology; return (source_id, target_id)."""
    vpc = create_test_vpc(ec2, cidr, f"isv-segment-boundary-{suffix}", enable_dns=True)
    resources["vpc_id"] = vpc.get("vpc_id")
    if not vpc["passed"]:
        raise RuntimeError(f"Failed to create VPC: {vpc.get('error')}")
    vpc_id = vpc["vpc_id"]

    igw_id = ec2.create_internet_gateway(TagSpecifications=_tag_spec("internet-gateway", f"isv-segment-{suffix}"))[
        "InternetGateway"
    ]["InternetGatewayId"]
    resources["igw_id"] = igw_id
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    az = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"][0][
        "ZoneName"
    ]
    prefix = cidr.split(".0.0/")[0]
    tenant_cidr, provider_cidr = f"{prefix}.1.0/24", f"{prefix}.2.0/24"
    tenant_subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=tenant_cidr, AvailabilityZone=az)["Subnet"]["SubnetId"]
    resources["subnet_ids"].append(tenant_subnet)
    provider_subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=provider_cidr, AvailabilityZone=az)["Subnet"][
        "SubnetId"
    ]
    resources["subnet_ids"].append(provider_subnet)

    # Only the tenant source needs a route out, to reach the SSM endpoints.
    ec2.modify_subnet_attribute(SubnetId=tenant_subnet, MapPublicIpOnLaunch={"Value": True})
    rtb_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    resources["rtb_id"] = rtb_id
    ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=tenant_subnet)

    tenant_sg = ec2.create_security_group(
        GroupName=f"isv-segment-tenant-{suffix}",
        Description="Tenant probe source (no inbound)",
        VpcId=vpc_id,
        TagSpecifications=_tag_spec("security-group", f"isv-segment-tenant-{suffix}"),
    )["GroupId"]
    resources["sg_ids"].append(tenant_sg)
    provider_sg = ec2.create_security_group(
        GroupName=f"isv-segment-provider-{suffix}",
        Description="Provider target (default-deny plus one allowed flow)",
        VpcId=vpc_id,
        TagSpecifications=_tag_spec("security-group", f"isv-segment-provider-{suffix}"),
    )["GroupId"]
    resources["sg_ids"].append(provider_sg)
    permissions = [
        {
            "IpProtocol": POSITIVE_CONTROL["protocol"],
            "FromPort": POSITIVE_CONTROL["port"],
            "ToPort": POSITIVE_CONTROL["port"],
            "IpRanges": [{"CidrIp": tenant_cidr, "Description": "SEC30 positive control"}],
        }
    ]
    if also_allow_port is not None:
        permissions.append(
            {
                "IpProtocol": "tcp",
                "FromPort": also_allow_port,
                "ToPort": also_allow_port,
                "IpRanges": [{"CidrIp": tenant_cidr, "Description": "Deliberate misconfiguration"}],
            }
        )
    ec2.authorize_security_group_ingress(GroupId=provider_sg, IpPermissions=permissions)

    role_name, profile_name = create_ssm_instance_profile(iam, "Temporary role for segment boundary probing")
    resources["role_name"], resources["profile_name"] = role_name, profile_name

    ami = get_amazon_linux_ami(ec2)
    if not ami:
        raise RuntimeError("Could not find Amazon Linux AMI")
    launch = {"ImageId": ami, "InstanceType": "t3.micro", "MinCount": 1, "MaxCount": 1}
    source_id = ec2.run_instances(
        **launch,
        SubnetId=tenant_subnet,
        SecurityGroupIds=[tenant_sg],
        IamInstanceProfile={"Name": profile_name},
        TagSpecifications=_tag_spec("instance", "isv-segment-tenant-source"),
    )["Instances"][0]["InstanceId"]
    resources["instance_ids"].append(source_id)
    target_id = ec2.run_instances(
        **launch,
        SubnetId=provider_subnet,
        SecurityGroupIds=[provider_sg],
        TagSpecifications=_tag_spec("instance", "isv-segment-provider-target"),
    )["Instances"][0]["InstanceId"]
    resources["instance_ids"].append(target_id)
    return source_id, target_id


@handle_aws_errors
def main() -> int:
    """Build the boundary, probe it, tear it down, and emit the JSON contract."""
    parser = argparse.ArgumentParser(description="Probe default-deny across a tenant-to-provider boundary")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.86.0.0/16", help="CIDR for the test VPC (/16)")
    parser.add_argument(
        "--also-allow-port",
        type=int,
        help="Deliberately break the boundary by also allowing this TCP port, to demonstrate a failing run",
    )
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    result: dict[str, Any] = {"success": False, "platform": "network", "test_name": "segment_boundary"}
    resources: dict[str, Any] = {
        "vpc_id": None,
        "igw_id": None,
        "rtb_id": None,
        "subnet_ids": [],
        "sg_ids": [],
        "instance_ids": [],
        "role_name": None,
        "profile_name": None,
    }

    try:
        source_id, target_id = build_boundary(
            ec2, iam, args.cidr, uuid.uuid4().hex[:8], resources, args.also_allow_port
        )
        ec2.get_waiter("instance_running").wait(InstanceIds=resources["instance_ids"])
        target_ip = ec2.describe_instances(InstanceIds=[target_id])["Reservations"][0]["Instances"][0][
            "PrivateIpAddress"
        ]
        if not wait_ssm_ready(ssm, source_id):
            raise RuntimeError("Tenant source SSM agent did not come online")

        # Retried because sshd on a freshly booted target may not be listening yet.
        control = dict(POSITIVE_CONTROL)
        for attempt in range(CONTROL_ATTEMPTS):
            control["result"] = probe(ssm, source_id, target_ip, control)
            if control["result"] == "connected" or attempt == CONTROL_ATTEMPTS - 1:
                break
            time.sleep(10)
        result["positive_control"] = control

        result["prohibited_flows"] = [
            {**flow, "result": probe(ssm, source_id, target_ip, flow)} for flow in PROHIBITED_FLOWS
        ]
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    finally:
        cleanup(ec2, iam, resources)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
