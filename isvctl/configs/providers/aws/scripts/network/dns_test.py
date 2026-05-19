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

"""Test localized DNS - custom DNS settings for internal domain resolution.

Creates a VPC with DNS enabled, a Route 53 private hosted zone,
adds a record pointing to a private endpoint, and verifies resolution.

Usage:
    python dns_test.py --region us-west-2 --cidr 10.89.0.0/16

Output JSON:
{
    "success": true,
    "platform": "network",
    "tests": {
        "create_vpc_with_dns": {"passed": true, "vpc_id": "vpc-xxx"},
        "create_hosted_zone": {"passed": true, "zone_id": "/hostedzone/Zxxx"},
        "create_dns_record": {"passed": true, "fqdn": "storage.internal.isv.test"},
        "verify_dns_settings": {"passed": true},
        "resolve_record": {"passed": true, "resolved_ip": "10.89.1.100"}
    }
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
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors
from common.vpc import delete_vpc

INTERNAL_DOMAIN = "internal.isv.test"


def create_vpc_with_dns(ec2: Any, cidr: str, name: str) -> dict[str, Any]:
    """Create a VPC with DNS support and hostnames enabled."""
    result: dict[str, Any] = {"passed": False}

    try:
        vpc = ec2.create_vpc(CidrBlock=cidr)
        vpc_id = vpc["Vpc"]["VpcId"]

        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {"Key": "Name", "Value": name},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        )

        waiter = ec2.get_waiter("vpc_available")
        waiter.wait(VpcIds=[vpc_id])

        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        result["passed"] = True
        result["vpc_id"] = vpc_id
        result["cidr"] = cidr
        result["message"] = f"Created DNS-enabled VPC {vpc_id}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def create_hosted_zone(route53: Any, vpc_id: str, region: str, domain: str) -> dict[str, Any]:
    """Create a Route 53 private hosted zone associated with the VPC."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = route53.create_hosted_zone(
            Name=domain,
            VPC={"VPCRegion": region, "VPCId": vpc_id},
            CallerReference=str(uuid.uuid4()),
            HostedZoneConfig={
                "Comment": "ISV test private hosted zone",
                "PrivateZone": True,
            },
        )

        zone_id = response["HostedZone"]["Id"]
        result["passed"] = True
        result["zone_id"] = zone_id
        result["domain"] = domain
        result["message"] = f"Created private hosted zone {zone_id} for {domain}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def create_dns_record(route53: Any, zone_id: str, fqdn: str, target_ip: str) -> dict[str, Any]:
    """Create a DNS A record in the private hosted zone."""
    result: dict[str, Any] = {"passed": False}

    try:
        route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": fqdn,
                            "Type": "A",
                            "TTL": 60,
                            "ResourceRecords": [{"Value": target_ip}],
                        },
                    }
                ]
            },
        )

        result["passed"] = True
        result["fqdn"] = fqdn
        result["target_ip"] = target_ip
        result["message"] = f"Created A record {fqdn} -> {target_ip}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def verify_dns_settings(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Verify DNS support and hostnames are enabled on the VPC."""
    result: dict[str, Any] = {"passed": False}

    try:
        dns_support = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsSupport")
        dns_hostnames = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsHostnames")

        support_enabled = dns_support["EnableDnsSupport"]["Value"]
        hostnames_enabled = dns_hostnames["EnableDnsHostnames"]["Value"]

        result["dns_support"] = support_enabled
        result["dns_hostnames"] = hostnames_enabled

        if support_enabled and hostnames_enabled:
            result["passed"] = True
            result["message"] = "DNS support and hostnames both enabled"
        else:
            result["error"] = f"DNS support={support_enabled}, hostnames={hostnames_enabled}"
    except ClientError as e:
        result["error"] = str(e)

    return result


def resolve_record(route53: Any, zone_id: str, fqdn: str, expected_ip: str) -> dict[str, Any]:
    """Verify the DNS record resolves to the expected IP via Route 53 API."""
    result: dict[str, Any] = {"passed": False}

    try:
        response = route53.list_resource_record_sets(
            HostedZoneId=zone_id,
            StartRecordName=fqdn,
            StartRecordType="A",
            MaxItems="1",
        )

        record_sets = response.get("ResourceRecordSets", [])
        found = False
        for rs in record_sets:
            if rs["Name"].rstrip(".") == fqdn.rstrip(".") and rs["Type"] == "A":
                records = rs.get("ResourceRecords", [])
                resolved_ips = [r["Value"] for r in records]
                if expected_ip in resolved_ips:
                    found = True
                    result["resolved_ip"] = expected_ip
                    result["all_ips"] = resolved_ips
                    break

        if found:
            result["passed"] = True
            result["message"] = f"{fqdn} resolves to {expected_ip}"
        else:
            result["error"] = f"Record {fqdn} not found or doesn't resolve to {expected_ip}"
    except ClientError as e:
        result["error"] = str(e)

    return result


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test localized DNS")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--cidr", default="10.89.0.0/16", help="CIDR for test VPC")
    parser.add_argument("--domain", default=INTERNAL_DOMAIN, help="Internal domain name")
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    route53 = boto3.client("route53", region_name=args.region)
    suffix = str(uuid.uuid4())[:8]

    storage_record = f"storage.{args.domain}"

    # Private endpoint IP within the VPC CIDR
    target_ip = args.cidr.replace(".0.0/16", ".1.100")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "status": "failed",
        "tests": {},
    }

    vpc_id = None
    zone_id = None

    try:
        # Test 1: Create VPC with DNS
        vpc_result = create_vpc_with_dns(ec2, args.cidr, f"isv-dns-test-{suffix}")
        result["tests"]["create_vpc_with_dns"] = vpc_result
        if not vpc_result["passed"]:
            raise RuntimeError("Failed to create VPC")
        vpc_id = vpc_result["vpc_id"]

        # Test 2: Create private hosted zone
        zone_result = create_hosted_zone(route53, vpc_id, args.region, args.domain)
        result["tests"]["create_hosted_zone"] = zone_result
        if not zone_result["passed"]:
            raise RuntimeError("Failed to create hosted zone")
        zone_id = zone_result["zone_id"]

        # Test 3: Create DNS record
        record_result = create_dns_record(route53, zone_id, storage_record, target_ip)
        result["tests"]["create_dns_record"] = record_result

        # Test 4: Verify DNS settings on VPC
        settings_result = verify_dns_settings(ec2, vpc_id)
        result["tests"]["verify_dns_settings"] = settings_result

        # Small delay for record propagation
        time.sleep(5)

        # Test 5: Resolve the record
        resolve_result = resolve_record(route53, zone_id, storage_record, target_ip)
        result["tests"]["resolve_record"] = resolve_result

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed
        result["status"] = "passed" if all_passed else "failed"

    except Exception as e:
        result["error"] = str(e)
    finally:
        # Cleanup: delete records, hosted zone, VPC
        if zone_id:
            try:
                route53.change_resource_record_sets(
                    HostedZoneId=zone_id,
                    ChangeBatch={
                        "Changes": [
                            {
                                "Action": "DELETE",
                                "ResourceRecordSet": {
                                    "Name": storage_record,
                                    "Type": "A",
                                    "TTL": 60,
                                    "ResourceRecords": [{"Value": target_ip}],
                                },
                            }
                        ]
                    },
                )
            except ClientError:
                pass
            try:
                route53.delete_hosted_zone(Id=zone_id)
            except ClientError:
                pass
        if vpc_id:
            delete_vpc(ec2, vpc_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
