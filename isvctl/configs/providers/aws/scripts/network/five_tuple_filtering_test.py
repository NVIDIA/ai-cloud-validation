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

"""Probe that security groups enforce each five-tuple match dimension (SEC15-01).

Builds one subnet with a source and a target instance, each carrying a
secondary private IP. Security groups then allow exactly one baseline flow,
TCP 8443 from the source's primary IP to the target's primary IP:

  - source egress:  tcp/8443 to the target's primary /32 (plus HTTPS for SSM)
  - target ingress: tcp/8443 from the source's primary /32

An AWS security group rule matches the remote address only - inbound rules
cannot name the destination and egress rules cannot name the source - so the
baseline is expressed as this pair, and each variant meets a rule that matches
on the dimension it changes. Source port is not probed: security groups cannot
match it.

The target runs a TCP listener and a UDP echo on 8443, so a variant the groups
let through connects (or is refused, on 8444) instead of timing out. Probes run
from the source over SSM and bind the source address explicitly.

Usage:
    python five_tuple_filtering_test.py --region us-west-2 --cidr 10.85.0.0/16
    python five_tuple_filtering_test.py --region us-west-2 --loosen source_ip  # expected to fail SEC15-01

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "five_tuple_filtering",
    "baseline": {"protocol": "tcp", "source_ip": "10.85.1.10", "destination_ip": "10.85.1.20",
                 "destination_port": 8443, "result": "connected"},
    "variants": [
        {"dimension": "protocol", "value": "udp", "result": "timeout"},
        {"dimension": "source_ip", "value": "10.85.1.11", "result": "timeout"},
        {"dimension": "destination_ip", "value": "10.85.1.21", "result": "timeout"},
        {"dimension": "destination_port", "value": 8444, "result": "timeout"}
    ]
}
"""

import argparse
import base64
import json
import os
import shlex
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.ec2 import create_ssm_instance_profile, delete_ssm_instance_profile, run_ssm_command, wait_ssm_ready_all
from common.errors import TRANSIENT_AWS_CODES, delete_with_retry, handle_aws_errors
from common.vpc import create_test_vpc

BASELINE_PROTOCOL = "tcp"
BASELINE_PORT = 8443
PROBE_TIMEOUT_SECONDS = 5
BASELINE_ATTEMPTS = 6
DIMENSIONS = ("protocol", "source_ip", "destination_ip", "destination_port")

# Runs on the target from user data: accept TCP and echo UDP on the baseline port.
RESPONDER = f"""\
import socket
import threading


def tcp():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", {BASELINE_PORT}))
    s.listen()
    while True:
        s.accept()[0].close()


threading.Thread(target=tcp, daemon=True).start()
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.bind(("0.0.0.0", {BASELINE_PORT}))
while True:
    data, peer = u.recvfrom(64)
    u.sendto(data, peer)
"""

TARGET_USER_DATA = f"""#!/bin/bash
cat > /opt/isv_five_tuple_responder.py <<'EOF'
{RESPONDER}EOF
systemd-run --unit isv-five-tuple-responder python3 /opt/isv_five_tuple_responder.py
"""

# Runs on the source over SSM: argv is protocol, source IP, destination IP, port.
PROBE = f"""\
import socket
import sys

protocol, source, destination, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
kind = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
s = socket.socket(socket.AF_INET, kind)
s.settimeout({PROBE_TIMEOUT_SECONDS})
try:
    s.bind((source, 0))
    s.connect((destination, port))
    if protocol == "udp":
        s.send(b"isv")
        s.recv(64)
    print("connected")
except socket.timeout:
    print("timeout")
except ConnectionRefusedError:
    print("refused")
except OSError:
    print("error")
"""
PROBE_B64 = base64.b64encode(PROBE.encode()).decode()


def _tag_spec(resource_type: str, name: str) -> list[dict[str, Any]]:
    """Return a TagSpecifications entry marking the resource as isvtest-owned."""
    tags = [{"Key": "Name", "Value": name}, {"Key": "CreatedBy", "Value": "isvtest"}]
    return [{"ResourceType": resource_type, "Tags": tags}]


def _get_al2023_ami(ec2: Any) -> str:
    """Return the latest standard (non-minimal) Amazon Linux 2023 x86_64 AMI, which ships python3 and SSM."""
    response = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-2023.*-x86_64"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    images = sorted(response.get("Images", []), key=lambda image: image["CreationDate"], reverse=True)
    if not images:
        raise RuntimeError("No Amazon Linux 2023 AMI found")
    return images[0]["ImageId"]


def baseline_rules(
    source_ip: str, target_ip: str, subnet_cidr: str, loosen: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (source egress, target ingress) permissions allowing only the baseline flow.

    ``loosen`` widens one dimension on both sides, so that dimension's variant
    gets through and SEC15-01 is expected to fail.
    """
    protocols = ["tcp", "udp"] if loosen == "protocol" else [BASELINE_PROTOCOL]
    to_port = BASELINE_PORT + 1 if loosen == "destination_port" else BASELINE_PORT
    source_range = subnet_cidr if loosen == "source_ip" else f"{source_ip}/32"
    destination_range = subnet_cidr if loosen == "destination_ip" else f"{target_ip}/32"

    def _permissions(cidr: str) -> list[dict[str, Any]]:
        """Return one permission per allowed protocol to/from ``cidr``."""
        return [
            {
                "IpProtocol": protocol,
                "FromPort": BASELINE_PORT,
                "ToPort": to_port,
                "IpRanges": [{"CidrIp": cidr, "Description": "SEC15 baseline flow"}],
            }
            for protocol in protocols
        ]

    return _permissions(destination_range), _permissions(source_range)


def probe(ssm: Any, source_id: str, protocol: str, source_ip: str, destination_ip: str, port: int) -> str:
    """Probe one flow from the source instance; return connected/refused/timeout/error."""
    args = shlex.join([protocol, source_ip, destination_ip, str(port)])
    ok, output = run_ssm_command(ssm, source_id, f"echo {PROBE_B64} | base64 -d | python3 - {args}")
    result = output.strip()
    return result if ok and result in ("connected", "refused", "timeout") else "error"


def ensure_secondary_ip(ssm: Any, instance_id: str, ip: str, prefix_length: int) -> None:
    """Make sure the OS answers on the ENI's secondary IP, so an allowed variant is not dropped by the host."""
    command = (
        f"ip -4 -o addr show | grep -qwF {ip} || "
        f"ip addr add {ip}/{prefix_length} dev $(ip -4 -o route show to default | awk '{{print $5}}' | head -1); "
        f"ip -4 -o addr show | grep -qwF {ip} && echo configured"
    )
    ok, output = run_ssm_command(ssm, instance_id, command)
    if not ok or "configured" not in output:
        raise RuntimeError(f"Could not configure secondary IP {ip} on {instance_id}: {output.strip()}")


def _private_ips(ec2: Any, instance_id: str) -> tuple[str, str]:
    """Return (primary, secondary) private IPs of an instance's primary ENI."""
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    addresses = instance["NetworkInterfaces"][0]["PrivateIpAddresses"]
    primary = next(a["PrivateIpAddress"] for a in addresses if a["Primary"])
    secondary = next(a["PrivateIpAddress"] for a in addresses if not a["Primary"])
    return primary, secondary


def _iam_remaining(iam: Any, role_name: str, profile_name: str) -> list[str]:
    """Return the SSM role/instance profile that still exist after teardown."""
    remaining = []
    for desc, lookup in (
        (f"IAM instance profile {profile_name}", lambda: iam.get_instance_profile(InstanceProfileName=profile_name)),
        (f"IAM role {role_name}", lambda: iam.get_role(RoleName=role_name)),
    ):
        try:
            lookup()
            remaining.append(desc)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
                remaining.append(f"{desc} (could not confirm deletion: {e})")
    return remaining


def cleanup(ec2: Any, iam: Any, resources: dict[str, Any]) -> list[str]:
    """Tear down everything the probe created, in dependency order; return resources left behind.

    Every step is attempted even when an earlier one fails, so one stuck
    resource does not leave the rest of the tree behind with it.
    """
    remaining: list[str] = []
    instance_ids = resources["instance_ids"]
    if instance_ids:
        terminated = delete_with_retry(
            ec2.terminate_instances, InstanceIds=instance_ids, resource_desc="probe instances"
        )
        try:
            ec2.get_waiter("instance_terminated").wait(InstanceIds=instance_ids)
        except Exception as e:
            terminated = False
            print(f"Warning: waiting for instance termination failed: {e}", file=sys.stderr)
        if not terminated:
            remaining.extend(f"instance {i}" for i in instance_ids)

    # ENIs of just-terminated instances can hold a security group for a few seconds.
    for sg_id in resources["sg_ids"]:
        if not delete_with_retry(
            ec2.delete_security_group,
            GroupId=sg_id,
            resource_desc=f"security group {sg_id}",
            attempts=6,
            backoff_seconds=5.0,
            transient_codes=TRANSIENT_AWS_CODES | {"DependencyViolation"},
        ):
            remaining.append(f"security group {sg_id}")
    for subnet_id in resources["subnet_ids"]:
        if not delete_with_retry(ec2.delete_subnet, SubnetId=subnet_id, resource_desc=f"subnet {subnet_id}"):
            remaining.append(f"subnet {subnet_id}")
    rtb_id = resources["rtb_id"]
    if rtb_id and not delete_with_retry(ec2.delete_route_table, RouteTableId=rtb_id, resource_desc="route table"):
        remaining.append(f"route table {rtb_id}")
    vpc_id = resources["vpc_id"]
    igw_id = resources["igw_id"]
    if igw_id:
        detached = delete_with_retry(
            ec2.detach_internet_gateway, InternetGatewayId=igw_id, VpcId=vpc_id, resource_desc="IGW"
        )
        if not (
            detached and delete_with_retry(ec2.delete_internet_gateway, InternetGatewayId=igw_id, resource_desc="IGW")
        ):
            remaining.append(f"internet gateway {igw_id}")
    if vpc_id and not delete_with_retry(ec2.delete_vpc, VpcId=vpc_id, resource_desc=f"VPC {vpc_id}"):
        remaining.append(f"VPC {vpc_id}")
    if resources["role_name"]:
        delete_ssm_instance_profile(iam, resources["role_name"], resources["profile_name"])
        remaining.extend(_iam_remaining(iam, resources["role_name"], resources["profile_name"]))
    return remaining


def build_topology(ec2: Any, iam: Any, cidr: str, suffix: str, resources: dict[str, Any]) -> dict[str, str]:
    """Create the VPC, groups, and instances; return ids and the subnet CIDR.

    The groups start with no baseline rule - it needs the instances' IPs, so
    it is authorized after launch. Both instances reach SSM over HTTPS through
    the internet gateway.
    """
    vpc = create_test_vpc(ec2, cidr, f"isv-five-tuple-{suffix}", enable_dns=True)
    resources["vpc_id"] = vpc.get("vpc_id")
    if not vpc["passed"]:
        raise RuntimeError(f"Failed to create VPC: {vpc.get('error')}")
    vpc_id = vpc["vpc_id"]

    igw_id = ec2.create_internet_gateway(TagSpecifications=_tag_spec("internet-gateway", f"isv-five-tuple-{suffix}"))[
        "InternetGateway"
    ]["InternetGatewayId"]
    resources["igw_id"] = igw_id
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    az = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"][0][
        "ZoneName"
    ]
    subnet_cidr = f"{cidr.split('.0.0/')[0]}.1.0/24"
    subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=az)["Subnet"]["SubnetId"]
    resources["subnet_ids"].append(subnet_id)
    rtb_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    resources["rtb_id"] = rtb_id
    ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)

    source_sg = ec2.create_security_group(
        GroupName=f"isv-five-tuple-source-{suffix}",
        Description="Five-tuple probe source (egress: SSM plus the baseline flow)",
        VpcId=vpc_id,
        TagSpecifications=_tag_spec("security-group", f"isv-five-tuple-source-{suffix}"),
    )["GroupId"]
    resources["sg_ids"].append(source_sg)
    ec2.revoke_security_group_egress(
        GroupId=source_sg, IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    )
    ec2.authorize_security_group_egress(
        GroupId=source_sg,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSM endpoints"}],
            }
        ],
    )
    target_sg = ec2.create_security_group(
        GroupName=f"isv-five-tuple-target-{suffix}",
        Description="Five-tuple probe target (ingress: the baseline flow only)",
        VpcId=vpc_id,
        TagSpecifications=_tag_spec("security-group", f"isv-five-tuple-target-{suffix}"),
    )["GroupId"]
    resources["sg_ids"].append(target_sg)

    role_name, profile_name = create_ssm_instance_profile(iam, "Temporary role for five-tuple filtering probes")
    resources["role_name"], resources["profile_name"] = role_name, profile_name

    ami = _get_al2023_ami(ec2)

    def _launch(sg_id: str, name: str, **extra: Any) -> str:
        """Launch one instance with a secondary private IP and a public IP for SSM."""
        instance_id = ec2.run_instances(
            ImageId=ami,
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
            NetworkInterfaces=[
                {
                    "DeviceIndex": 0,
                    "SubnetId": subnet_id,
                    "Groups": [sg_id],
                    "SecondaryPrivateIpAddressCount": 1,
                    "AssociatePublicIpAddress": True,
                }
            ],
            IamInstanceProfile={"Name": profile_name},
            TagSpecifications=_tag_spec("instance", name),
            **extra,
        )["Instances"][0]["InstanceId"]
        resources["instance_ids"].append(instance_id)
        return instance_id

    source_id = _launch(source_sg, "isv-five-tuple-source")
    target_id = _launch(target_sg, "isv-five-tuple-target", UserData=TARGET_USER_DATA)
    return {
        "source_id": source_id,
        "target_id": target_id,
        "source_sg": source_sg,
        "target_sg": target_sg,
        "subnet_cidr": subnet_cidr,
    }


@handle_aws_errors
def main() -> int:
    """Build the topology, probe the baseline and its variants, tear down, and emit the JSON contract."""
    parser = argparse.ArgumentParser(description="Probe five-tuple filtering enforcement with security groups")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.85.0.0/16", help="CIDR for the test VPC (/16)")
    parser.add_argument(
        "--loosen",
        choices=DIMENSIONS,
        help="Deliberately widen the baseline rule in one dimension, to demonstrate a failing run",
    )
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    result: dict[str, Any] = {"success": False, "platform": "network", "test_name": "five_tuple_filtering"}
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
        topology = build_topology(ec2, iam, args.cidr, uuid.uuid4().hex[:8], resources)
        source_id, target_id = topology["source_id"], topology["target_id"]
        ec2.get_waiter("instance_running").wait(InstanceIds=[source_id, target_id])
        source_ip, source_secondary = _private_ips(ec2, source_id)
        target_ip, target_secondary = _private_ips(ec2, target_id)

        egress, ingress = baseline_rules(source_ip, target_ip, topology["subnet_cidr"], args.loosen)
        ec2.authorize_security_group_egress(GroupId=topology["source_sg"], IpPermissions=egress)
        ec2.authorize_security_group_ingress(GroupId=topology["target_sg"], IpPermissions=ingress)

        offline = wait_ssm_ready_all(ssm, [source_id, target_id], timeout=300)
        if offline:
            raise RuntimeError(f"SSM agent did not come online on {', '.join(offline)}")
        prefix_length = int(topology["subnet_cidr"].split("/")[1])
        ensure_secondary_ip(ssm, source_id, source_secondary, prefix_length)
        ensure_secondary_ip(ssm, target_id, target_secondary, prefix_length)

        # Retried because the target's responder starts from user data, which can finish after SSM is online.
        baseline = {
            "protocol": BASELINE_PROTOCOL,
            "source_ip": source_ip,
            "destination_ip": target_ip,
            "destination_port": BASELINE_PORT,
        }
        for attempt in range(BASELINE_ATTEMPTS):
            baseline["result"] = probe(ssm, source_id, BASELINE_PROTOCOL, source_ip, target_ip, BASELINE_PORT)
            if baseline["result"] == "connected" or attempt == BASELINE_ATTEMPTS - 1:
                break
            time.sleep(10)
        result["baseline"] = baseline

        variants = [
            ("protocol", "udp", ("udp", source_ip, target_ip, BASELINE_PORT)),
            ("source_ip", source_secondary, (BASELINE_PROTOCOL, source_secondary, target_ip, BASELINE_PORT)),
            ("destination_ip", target_secondary, (BASELINE_PROTOCOL, source_ip, target_secondary, BASELINE_PORT)),
            ("destination_port", BASELINE_PORT + 1, (BASELINE_PROTOCOL, source_ip, target_ip, BASELINE_PORT + 1)),
        ]
        result["variants"] = [
            {"dimension": dimension, "value": value, "result": probe(ssm, source_id, *flow)}
            for dimension, value, flow in variants
        ]
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    finally:
        remaining = cleanup(ec2, iam, resources)
        if remaining:
            result["cleanup_errors"] = [f"not deleted: {r}" for r in remaining]
            cleanup_error = f"Cleanup failed, resources remain: {', '.join(remaining)}"
            result["error"] = f"{result['error']}; {cleanup_error}" if result.get("error") else cleanup_error
            result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
