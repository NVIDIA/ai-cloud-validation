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

"""Test that the egress IP observed by external services is stable (DMS05-01).

NVIDIA cloud services use IP allowlists, so workloads that call out to them
must present a stable egress IP. This script launches a test instance with
direct internet egress (auto-assigned public IP via IGW), probes
``https://api.ipify.org`` (or a configurable endpoint) N times from the
instance, and verifies every probe returned the same address.

Usage:
    python stable_egress_ip_test.py --region us-west-2 --cidr 10.100.0.0/16 \\
        --probes 3 --interval-seconds 2 --endpoint https://api.ipify.org

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "stable_egress_ip",
    "tests": {
        "create_instance":  {"passed": true},
        "probe_egress_ip":  {"passed": true, "probes": 3},
        "egress_ip_stable": {"passed": true}
    }
}
"""

import argparse
import ipaddress
import json
import os
import shlex
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError, WaiterError
from common.ec2 import create_key_pair, create_security_group, get_amazon_linux_ami
from common.errors import delete_with_retry, handle_aws_errors
from common.ssh_utils import ssh_run, wait_for_ssh
from common.vpc import create_test_vpc, delete_vpc

TEST_NAME = "stable_egress_ip"
TEST_NAMES = ("create_instance", "probe_egress_ip", "egress_ip_stable")


def base_result() -> dict[str, Any]:
    """Build the minimal provider-neutral result skeleton."""
    return {
        "success": False,
        "platform": "network",
        "test_name": TEST_NAME,
        "tests": {test_name: {"passed": False} for test_name in TEST_NAMES},
    }


def public_test_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return only validation-relevant fields from an internal subtest result."""
    public: dict[str, Any] = {"passed": bool(result.get("passed", False))}
    if "probes" in result:
        public["probes"] = result["probes"]
    if not public["passed"] and "error" in result:
        public["error"] = result["error"]
    return public


def derive_subnet_cidr(cidr: str) -> str:
    """Return the first subnet CIDR to create inside the requested VPC CIDR."""
    network = ipaddress.ip_network(cidr)
    if network.version == 4 and network.prefixlen < 24:
        return str(next(network.subnets(new_prefix=24)))
    return str(network)


def create_internet_routing(ec2: Any, vpc_id: str, subnet_id: str, name: str, routing: dict[str, str]) -> None:
    """Create IGW + route table + default route + association.

    Each created resource's ID is written into ``routing`` immediately so a
    mid-function failure still leaves the caller's finally block able to
    clean up everything that was created. A function that built and returned
    the IDs only at the end would silently leak the IGW (and possibly the
    route table) whenever a later step raised.
    """
    igw = ec2.create_internet_gateway(
        TagSpecifications=[
            {
                "ResourceType": "internet-gateway",
                "Tags": [{"Key": "Name", "Value": name}, {"Key": "CreatedBy", "Value": "isvtest"}],
            }
        ]
    )
    routing["igw_id"] = igw["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=routing["igw_id"], VpcId=vpc_id)

    rt = ec2.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "route-table",
                "Tags": [{"Key": "Name", "Value": name}, {"Key": "CreatedBy", "Value": "isvtest"}],
            }
        ],
    )
    routing["route_table_id"] = rt["RouteTable"]["RouteTableId"]
    ec2.create_route(
        RouteTableId=routing["route_table_id"],
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=routing["igw_id"],
    )
    assoc = ec2.associate_route_table(RouteTableId=routing["route_table_id"], SubnetId=subnet_id)
    routing["association_id"] = assoc["AssociationId"]


def launch_instance(
    ec2: Any,
    subnet_id: str,
    sg_id: str,
    key_name: str,
    name: str,
) -> dict[str, Any]:
    """Launch an EC2 instance with an auto-assigned public IP."""
    result: dict[str, Any] = {"passed": False}

    ami_id = get_amazon_linux_ami(ec2)
    if not ami_id:
        result["error"] = "Could not find Amazon Linux AMI"
        return result

    try:
        response = ec2.run_instances(
            ImageId=ami_id,
            InstanceType="t3.micro",
            KeyName=key_name,
            MinCount=1,
            MaxCount=1,
            NetworkInterfaces=[
                {
                    "DeviceIndex": 0,
                    "SubnetId": subnet_id,
                    "Groups": [sg_id],
                    "AssociatePublicIpAddress": True,
                }
            ],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": name},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        instance_id = response["Instances"][0]["InstanceId"]

        ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

        desc = ec2.describe_instances(InstanceIds=[instance_id])
        instance = desc["Reservations"][0]["Instances"][0]
        public_ip = instance.get("PublicIpAddress")

        if not public_ip:
            result["error"] = "Instance running but no public IP assigned"
            return result

        result["passed"] = True
        result["instance_id"] = instance_id
        result["public_ip"] = public_ip
        result["message"] = f"Launched instance {instance_id} with public IP {public_ip}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def probe_egress_ip(
    public_ip: str,
    key_file: str,
    endpoint: str,
    probes: int,
    interval_seconds: float,
    ssh_user: str = "ec2-user",
) -> dict[str, Any]:
    """Probe the egress IP via SSH + curl, ``probes`` times, ``interval_seconds`` apart."""
    result: dict[str, Any] = {
        "passed": False,
        "ips": [],
        "endpoint": endpoint,
        "probes": probes,
    }

    # max_attempts * (subprocess timeout 15s + interval 5s) must fit comfortably
    # under the orchestrator step timeout (600s). On SIGKILL the finally block
    # does not run and every AWS resource above this point leaks.
    if not wait_for_ssh(public_ip, ssh_user, key_file, max_attempts=20, interval=5):
        result["error"] = f"SSH not reachable on {public_ip} after 20 attempts"
        return result

    # endpoint is shlex-quoted so a malicious or typo'd --endpoint can't smuggle
    # shell metachars onto the remote login shell ssh invokes for us.
    cmd = f"curl -s --max-time 5 {shlex.quote(endpoint)}"
    for attempt in range(1, probes + 1):
        if attempt > 1:
            time.sleep(interval_seconds)
        exit_code, stdout, stderr = ssh_run(public_ip, ssh_user, key_file, cmd, timeout=15)
        if exit_code != 0:
            result["error"] = f"probe {attempt}/{probes} failed (exit={exit_code}): {stderr.strip() or stdout.strip()}"
            return result
        ip = stdout.strip()
        if not ip:
            result["error"] = f"probe {attempt}/{probes} returned empty response"
            return result
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            result["error"] = f"probe {attempt}/{probes} returned non-IP value: {ip!r}"
            return result
        result["ips"].append(ip)

    result["passed"] = True
    result["message"] = f"Collected {probes} egress IP probes from {endpoint}"
    return result


def check_egress_ip_stable(ips: list[str]) -> dict[str, Any]:
    """Assert every probe returned the same egress IP."""
    distinct = sorted(set(ips))
    result: dict[str, Any] = {"passed": False, "distinct": len(distinct)}
    if not ips:
        result["error"] = "No probes collected"
        return result
    if len(distinct) == 1:
        result["passed"] = True
        result["ip"] = distinct[0]
        result["message"] = f"Egress IP {distinct[0]} stable across {len(ips)} probes"
    else:
        result["error"] = f"Egress IP changed across probes: {', '.join(distinct)}"
    return result


@handle_aws_errors
def main() -> int:
    """Run the AWS stable egress IP test and print its JSON result."""
    parser = argparse.ArgumentParser(description="Test stable egress IP (DMS05-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.100.0.0/16", help="CIDR for test VPC")
    parser.add_argument("--probes", type=int, default=3, help="Number of egress IP probes")
    parser.add_argument("--interval-seconds", type=float, default=2.0, help="Delay between probes")
    parser.add_argument(
        "--endpoint",
        default="https://api.ipify.org",
        help="IP-discovery endpoint that echoes the caller's egress IP",
    )
    parser.add_argument("--ssh-user", default="ec2-user", help="SSH user for the AMI")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    suffix = str(uuid.uuid4())[:8]
    name = f"isv-stable-egress-ip-{suffix}"

    result = base_result()

    vpc_id = None
    subnet_id = None
    sg_id = None
    instance_id = None
    routing: dict[str, str] = {}
    key_name: str | None = None
    key_file: str | None = None

    try:
        vpc_result = create_test_vpc(ec2, args.cidr, name)
        if not vpc_result["passed"]:
            raise RuntimeError(vpc_result.get("error", "Failed to create VPC"))
        vpc_id = vpc_result["vpc_id"]

        azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
        az = azs["AvailabilityZones"][0]["ZoneName"]
        subnet_cidr = derive_subnet_cidr(args.cidr)
        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=az)
        subnet_id = subnet["Subnet"]["SubnetId"]

        create_internet_routing(ec2, vpc_id, subnet_id, name, routing)

        sg_id = create_security_group(ec2, vpc_id, f"{name}-sg", description="Stable egress IP test SG")

        key_name = f"{name}-key"
        key_file = create_key_pair(ec2, key_name)

        create_result = launch_instance(ec2, subnet_id, sg_id, key_name, name)
        result["tests"]["create_instance"] = public_test_result(create_result)
        if not create_result["passed"]:
            raise RuntimeError("Failed to launch instance")
        instance_id = create_result["instance_id"]
        public_ip = create_result["public_ip"]

        probe_result = probe_egress_ip(
            public_ip=public_ip,
            key_file=key_file,
            endpoint=args.endpoint,
            probes=args.probes,
            interval_seconds=args.interval_seconds,
            ssh_user=args.ssh_user,
        )
        result["tests"]["probe_egress_ip"] = public_test_result(probe_result)
        if not probe_result["passed"]:
            raise RuntimeError("Egress IP probing failed")

        stable_result = check_egress_ip_stable(probe_result["ips"])
        result["tests"]["egress_ip_stable"] = public_test_result(stable_result)

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed

    except Exception as e:
        result["error"] = str(e)
    finally:
        if instance_id:
            if delete_with_retry(
                ec2.terminate_instances,
                InstanceIds=[instance_id],
                resource_desc=f"instance {instance_id}",
            ):
                try:
                    ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
                except (ClientError, WaiterError):
                    pass
            time.sleep(5)
        if key_name:
            delete_with_retry(
                ec2.delete_key_pair,
                KeyName=key_name,
                resource_desc=f"key pair {key_name}",
            )
        if key_file and os.path.exists(key_file):
            try:
                os.remove(key_file)
            except OSError:
                pass
        if sg_id:
            delete_with_retry(
                ec2.delete_security_group,
                GroupId=sg_id,
                resource_desc=f"security group {sg_id}",
            )
        if routing.get("association_id"):
            delete_with_retry(
                ec2.disassociate_route_table,
                AssociationId=routing["association_id"],
                resource_desc=f"route table association {routing['association_id']}",
            )
        if routing.get("route_table_id"):
            delete_with_retry(
                ec2.delete_route_table,
                RouteTableId=routing["route_table_id"],
                resource_desc=f"route table {routing['route_table_id']}",
            )
        if routing.get("igw_id") and vpc_id:
            delete_with_retry(
                ec2.detach_internet_gateway,
                InternetGatewayId=routing["igw_id"],
                VpcId=vpc_id,
                resource_desc=f"detach IGW {routing['igw_id']}",
            )
            delete_with_retry(
                ec2.delete_internet_gateway,
                InternetGatewayId=routing["igw_id"],
                resource_desc=f"internet gateway {routing['igw_id']}",
            )
        if subnet_id:
            delete_with_retry(
                ec2.delete_subnet,
                SubnetId=subnet_id,
                resource_desc=f"subnet {subnet_id}",
            )
        if vpc_id:
            delete_vpc(ec2, vpc_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
