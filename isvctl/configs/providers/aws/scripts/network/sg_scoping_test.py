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

"""Test security group rule scoping at workload, node, subnet, or service level.

AWS mapping:
  - workload/node: SGs attach per-ENI, so rules scope to individual
    instances.  We create a VPC with two ENIs, apply an SG to only one,
    and verify the rule is present on the target but absent on the other.
  - subnet: NACLs scope to subnets.  We create two subnets, apply a
    custom NACL with a deny rule to one, and verify the other subnet
    still uses the default (allow-all) NACL.
  - service: SGs attach to the ENIs of a VPC interface endpoint
    (PrivateLink), scoping an HTTPS rule to that one service endpoint.
    We create an EC2 interface endpoint plus an unrelated ENI in the same
    subnet, and verify the SG is attached only to the endpoint's ENIs.

Usage:
    python sg_scoping_test.py --region us-west-2 --scope workload
    python sg_scoping_test.py --region us-west-2 --scope node
    python sg_scoping_test.py --region us-west-2 --scope subnet
    python sg_scoping_test.py --region us-west-2 --scope service
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
from common.errors import ALREADY_GONE_CODES, handle_aws_errors
from common.vpc import cleanup_vpc_resources, create_test_vpc

CIDR = "10.85.0.0/16"
SUBNET_A_CIDR = "10.85.1.0/24"
SUBNET_B_CIDR = "10.85.2.0/24"
ALREADY_GONE_CLEANUP_CODES = ALREADY_GONE_CODES | frozenset({"InvalidNetworkInterfaceID.NotFound"})


def _get_az(ec2: Any, region: str) -> str:
    """Return the first available AZ in the region."""
    azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"]
    if not azs:
        msg = f"No available AZ found in region {region}"
        raise ValueError(msg)
    return azs[0]["ZoneName"]


def test_workload_or_node_scoping(ec2: Any, vpc_id: str, az: str, scope: str) -> dict[str, Any]:
    """Verify SG rules scope to a single ENI (workload/node level)."""
    results: dict[str, Any] = {}
    sg_id = None
    subnet_id = None
    eni_target = None
    eni_other = None
    cleanup_errors: list[str] = []
    tag = f"isv-sg-scope-{scope}-{uuid.uuid4().hex[:6]}"

    apply_key = f"apply_{scope}_rule"
    allowed_key = f"{'workload' if scope == 'workload' else 'target_node'}_allowed"
    blocked_key = f"other_{'workload' if scope == 'workload' else 'node'}_blocked"
    expected_keys = ["create_sg", apply_key, allowed_key, blocked_key]

    try:
        sg = ec2.create_security_group(
            GroupName=tag,
            Description=f"SG scoping test ({scope})",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}],
                }
            ],
        )
        sg_id = sg["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                }
            ],
        )
        results["create_sg"] = {"passed": True}

        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=SUBNET_A_CIDR, AvailabilityZone=az)
        subnet_id = subnet["Subnet"]["SubnetId"]

        eni_t = ec2.create_network_interface(SubnetId=subnet_id, Groups=[sg_id])
        eni_target = eni_t["NetworkInterface"]["NetworkInterfaceId"]

        eni_o = ec2.create_network_interface(SubnetId=subnet_id)
        eni_other = eni_o["NetworkInterface"]["NetworkInterfaceId"]

        results[apply_key] = {"passed": True}

        enis_info = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_target, eni_other])
        by_id = {e["NetworkInterfaceId"]: e for e in enis_info["NetworkInterfaces"]}
        target_sgs = [g["GroupId"] for g in by_id[eni_target]["Groups"]]
        other_sgs = [g["GroupId"] for g in by_id[eni_other]["Groups"]]

        if sg_id in target_sgs:
            results[allowed_key] = {"passed": True, "message": f"SG {sg_id} attached to target ENI"}
        else:
            results[allowed_key] = {"passed": False, "error": "SG not attached to target ENI"}

        if sg_id not in other_sgs:
            results[blocked_key] = {"passed": True, "message": "SG not on other ENI (scoped correctly)"}
        else:
            results[blocked_key] = {"passed": False, "error": "SG leaked to other ENI"}

    except ClientError as e:
        for key in expected_keys:
            results.setdefault(key, {"passed": False, "error": str(e)})
    finally:
        for eni_id in [eni_target, eni_other]:
            if eni_id:
                try:
                    ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                except ClientError as e:
                    cleanup_errors.append(f"delete ENI {eni_id}: {e}")
        if subnet_id:
            try:
                ec2.delete_subnet(SubnetId=subnet_id)
            except ClientError as e:
                cleanup_errors.append(f"delete subnet {subnet_id}: {e}")
        if sg_id:
            try:
                ec2.delete_security_group(GroupId=sg_id)
            except ClientError as e:
                cleanup_errors.append(f"delete SG {sg_id}: {e}")

    results["cleanup"] = {"passed": not cleanup_errors}
    if cleanup_errors:
        results["cleanup"]["error"] = "; ".join(cleanup_errors)
    return results


def test_subnet_scoping(ec2: Any, vpc_id: str, az: str) -> dict[str, Any]:
    """Verify NACL rules scope to a single subnet."""
    results: dict[str, Any] = {}
    subnet_a = None
    subnet_b = None
    nacl_id = None
    cleanup_errors: list[str] = []

    try:
        # Subnet scoping in AWS is enforced by NACLs rather than SGs; record
        # this as the "create_sg" step the validation contract expects.
        results["create_sg"] = {"passed": True, "message": "Using NACLs for subnet-level scoping in AWS"}

        sa = ec2.create_subnet(VpcId=vpc_id, CidrBlock=SUBNET_A_CIDR, AvailabilityZone=az)
        subnet_a = sa["Subnet"]["SubnetId"]
        sb = ec2.create_subnet(VpcId=vpc_id, CidrBlock=SUBNET_B_CIDR, AvailabilityZone=az)
        subnet_b = sb["Subnet"]["SubnetId"]

        # Create custom NACL with a deny rule and associate to subnet A
        nacl = ec2.create_network_acl(VpcId=vpc_id)
        nacl_id = nacl["NetworkAcl"]["NetworkAclId"]
        ec2.create_tags(
            Resources=[nacl_id],
            Tags=[{"Key": "CreatedBy", "Value": "isvtest"}],
        )
        ec2.create_network_acl_entry(
            NetworkAclId=nacl_id,
            RuleNumber=100,
            Protocol="-1",
            RuleAction="deny",
            Egress=False,
            CidrBlock="0.0.0.0/0",
        )
        ec2.replace_network_acl_association(
            AssociationId=_get_nacl_assoc(ec2, vpc_id, subnet_a),
            NetworkAclId=nacl_id,
        )
        results["apply_subnet_rule"] = {"passed": True}

        nacls_both = ec2.describe_network_acls(
            Filters=[{"Name": "association.subnet-id", "Values": [subnet_a, subnet_b]}]
        )["NetworkAcls"]
        nacls_by_subnet: dict[str, set[str]] = {subnet_a: set(), subnet_b: set()}
        for nacl in nacls_both:
            for assoc in nacl.get("Associations", []):
                sid = assoc.get("SubnetId")
                if sid in nacls_by_subnet:
                    nacls_by_subnet[sid].add(nacl["NetworkAclId"])

        if nacl_id in nacls_by_subnet[subnet_a]:
            results["subnet_allowed"] = {"passed": True, "message": "Custom NACL applied to target subnet"}
        else:
            results["subnet_allowed"] = {"passed": False, "error": "Custom NACL not on target subnet"}

        if nacl_id not in nacls_by_subnet[subnet_b]:
            results["other_subnet_blocked"] = {
                "passed": True,
                "message": "Custom NACL not on other subnet (scoped correctly)",
            }
        else:
            results["other_subnet_blocked"] = {"passed": False, "error": "NACL leaked to other subnet"}

    except ClientError as e:
        for key in ["create_sg", "apply_subnet_rule", "subnet_allowed", "other_subnet_blocked"]:
            results.setdefault(key, {"passed": False, "error": str(e)})
    finally:
        # Custom NACL cannot be deleted while associated; swap subnet_a back
        # to the VPC's default NACL first.
        if nacl_id and subnet_a:
            try:
                default_nacl = _get_default_nacl(ec2, vpc_id)
                if default_nacl:
                    assoc = _get_nacl_assoc_for_nacl(ec2, nacl_id, subnet_a)
                    if assoc:
                        ec2.replace_network_acl_association(
                            AssociationId=assoc,
                            NetworkAclId=default_nacl,
                        )
            except ClientError as e:
                cleanup_errors.append(f"restore default NACL for {subnet_a}: {e}")
        if nacl_id:
            try:
                ec2.delete_network_acl(NetworkAclId=nacl_id)
            except ClientError as e:
                cleanup_errors.append(f"delete NACL {nacl_id}: {e}")
        for sid in [subnet_a, subnet_b]:
            if sid:
                try:
                    ec2.delete_subnet(SubnetId=sid)
                except ClientError as e:
                    cleanup_errors.append(f"delete subnet {sid}: {e}")

    results["cleanup"] = {"passed": not cleanup_errors}
    if cleanup_errors:
        results["cleanup"]["error"] = "; ".join(cleanup_errors)
    return results


def test_service_scoping(ec2: Any, vpc_id: str, az: str, region: str) -> dict[str, Any]:
    """Verify SG rules scope to a single VPC interface endpoint (service level)."""
    results: dict[str, Any] = {}
    sg_id = None
    subnet_id = None
    endpoint_id = None
    eni_other = None
    cleanup_errors: list[str] = []
    expected_keys = [
        "create_sg",
        "apply_service_rule",
        "service_endpoint_allowed",
        "other_endpoint_blocked",
    ]
    tag = f"isv-sg-scope-service-{uuid.uuid4().hex[:6]}"

    try:
        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=SUBNET_A_CIDR, AvailabilityZone=az)
        subnet_id = subnet["Subnet"]["SubnetId"]

        sg = ec2.create_security_group(
            GroupName=tag,
            Description="SG scoping test (service)",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}],
                }
            ],
        )
        sg_id = sg["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                }
            ],
        )
        results["create_sg"] = {"passed": True}

        endpoint = ec2.create_vpc_endpoint(
            VpcId=vpc_id,
            ServiceName=f"com.amazonaws.{region}.ec2",
            VpcEndpointType="Interface",
            SubnetIds=[subnet_id],
            SecurityGroupIds=[sg_id],
            PrivateDnsEnabled=False,
            TagSpecifications=[
                {
                    "ResourceType": "vpc-endpoint",
                    "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}],
                }
            ],
        )
        endpoint_id = endpoint["VpcEndpoint"]["VpcEndpointId"]
        results["apply_service_rule"] = {"passed": True}

        eni_o = ec2.create_network_interface(SubnetId=subnet_id)
        eni_other = eni_o["NetworkInterface"]["NetworkInterfaceId"]

        endpoint_eni_ids = _wait_for_endpoint_enis(ec2, endpoint_id)
        if not endpoint_eni_ids:
            results["service_endpoint_allowed"] = {
                "passed": False,
                "error": "VPC endpoint did not expose any ENIs",
            }
            results["other_endpoint_blocked"] = {
                "passed": False,
                "error": "Cannot verify SG scoping: VPC endpoint has no ENIs",
            }
        else:
            enis_info = ec2.describe_network_interfaces(NetworkInterfaceIds=[*endpoint_eni_ids, eni_other])
            by_id = {e["NetworkInterfaceId"]: e for e in enis_info["NetworkInterfaces"]}
            endpoint_attached = all(
                sg_id in [g["GroupId"] for g in by_id[eni_id]["Groups"]] for eni_id in endpoint_eni_ids
            )
            other_sgs = [g["GroupId"] for g in by_id[eni_other]["Groups"]]

            if endpoint_attached:
                results["service_endpoint_allowed"] = {
                    "passed": True,
                    "message": f"SG {sg_id} attached to all {len(endpoint_eni_ids)} endpoint ENI(s)",
                }
            else:
                results["service_endpoint_allowed"] = {
                    "passed": False,
                    "error": f"SG {sg_id} not on every endpoint ENI",
                }

            if sg_id not in other_sgs:
                results["other_endpoint_blocked"] = {
                    "passed": True,
                    "message": "SG not on unrelated ENI (scoped to service)",
                }
            else:
                results["other_endpoint_blocked"] = {
                    "passed": False,
                    "error": "SG leaked to unrelated ENI",
                }

    except ClientError as e:
        for key in expected_keys:
            results.setdefault(key, {"passed": False, "error": str(e)})
    finally:
        endpoint_delete_started = False
        endpoint_deleted = endpoint_id is None
        if endpoint_id:
            try:
                delete_result = ec2.delete_vpc_endpoints(VpcEndpointIds=[endpoint_id])
                unsuccessful = delete_result.get("Unsuccessful", [])
                if unsuccessful:
                    msg = f"delete_vpc_endpoints reported unsuccessful entries: {unsuccessful}"
                    raise RuntimeError(msg)
                endpoint_delete_started = True
            except Exception as e:
                cleanup_errors.append(f"delete VPC endpoint {endpoint_id}: {e}")
        if eni_other:
            eni_delete_error = _delete_with_dependency_retry(
                ec2.delete_network_interface,
                resource_desc=f"ENI {eni_other}",
                NetworkInterfaceId=eni_other,
            )
            if eni_delete_error:
                cleanup_errors.append(f"delete ENI {eni_other}: {eni_delete_error}")
        if endpoint_id and endpoint_delete_started:
            try:
                _wait_for_endpoint_deletion(ec2, endpoint_id)
                endpoint_deleted = True
            except Exception as e:
                cleanup_errors.append(f"delete VPC endpoint {endpoint_id}: {e}")
        dependency_attempts = 12 if endpoint_deleted else 1
        if subnet_id:
            subnet_delete_error = _delete_with_dependency_retry(
                ec2.delete_subnet,
                resource_desc=f"subnet {subnet_id}",
                attempts=dependency_attempts,
                SubnetId=subnet_id,
            )
            if subnet_delete_error:
                cleanup_errors.append(f"delete subnet {subnet_id}: {subnet_delete_error}")
        if sg_id:
            sg_delete_error = _delete_with_dependency_retry(
                ec2.delete_security_group,
                resource_desc=f"SG {sg_id}",
                attempts=dependency_attempts,
                GroupId=sg_id,
            )
            if sg_delete_error:
                cleanup_errors.append(f"delete SG {sg_id}: {sg_delete_error}")

    results["cleanup"] = {"passed": not cleanup_errors}
    if cleanup_errors:
        results["cleanup"]["error"] = "; ".join(cleanup_errors)
    return results


def _wait_for_endpoint_enis(
    ec2: Any,
    endpoint_id: str,
    attempts: int = 30,
    delay: float = 2.0,
) -> list[str]:
    """Poll the VPC endpoint until it reports its ENIs (or attempts run out)."""
    for _ in range(attempts):
        resp = ec2.describe_vpc_endpoints(VpcEndpointIds=[endpoint_id])
        endpoints = resp.get("VpcEndpoints", [])
        if endpoints and endpoints[0].get("NetworkInterfaceIds"):
            return list(endpoints[0]["NetworkInterfaceIds"])
        time.sleep(delay)
    return []


def _wait_for_endpoint_deletion(
    ec2: Any,
    endpoint_id: str,
    attempts: int = 90,
    delay: float = 2.0,
) -> None:
    """Poll until the VPC endpoint is gone so dependent resources can be deleted."""
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
    msg = f"Timed out waiting for VPC endpoint {endpoint_id} deletion after {attempts} attempts"
    raise TimeoutError(msg)


def _delete_with_dependency_retry(
    fn: Any,
    *,
    resource_desc: str,
    attempts: int = 12,
    delay: float = 5.0,
    **kwargs: Any,
) -> str | None:
    """Delete a resource that may briefly depend on an AWS-managed ENI."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            fn(**kwargs)
            return None
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ALREADY_GONE_CLEANUP_CODES:
                return None
            last_error = e
            if code == "DependencyViolation" and attempt < attempts:
                time.sleep(delay)
                continue
            return str(e)
    return str(last_error) if last_error else f"{resource_desc} delete did not complete"


def _get_nacl_assoc(ec2: Any, vpc_id: str, subnet_id: str) -> str:
    """Get the NACL association ID for a subnet."""
    nacls = ec2.describe_network_acls(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "association.subnet-id", "Values": [subnet_id]},
        ]
    )["NetworkAcls"]
    for nacl in nacls:
        for assoc in nacl.get("Associations", []):
            if assoc.get("SubnetId") == subnet_id:
                return assoc["NetworkAclAssociationId"]
    msg = f"No NACL association for subnet {subnet_id}"
    raise ValueError(msg)


def _get_nacl_assoc_for_nacl(ec2: Any, nacl_id: str, subnet_id: str) -> str | None:
    """Get the association ID for a specific NACL + subnet pair."""
    nacls = ec2.describe_network_acls(NetworkAclIds=[nacl_id])["NetworkAcls"]
    for nacl in nacls:
        for assoc in nacl.get("Associations", []):
            if assoc.get("SubnetId") == subnet_id:
                return assoc["NetworkAclAssociationId"]
    return None


def _get_default_nacl(ec2: Any, vpc_id: str) -> str | None:
    """Get the default NACL ID for a VPC."""
    nacls = ec2.describe_network_acls(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "default", "Values": ["true"]},
        ]
    )["NetworkAcls"]
    return nacls[0]["NetworkAclId"] if nacls else None


@handle_aws_errors
def main() -> int:
    """Run SG rule scoping test for the given scope and emit JSON result."""
    parser = argparse.ArgumentParser(description="Test SG rule scoping levels")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--scope", required=True, choices=["workload", "node", "subnet", "service"])
    args = parser.parse_args()

    ec2 = boto3.client("ec2", region_name=args.region)
    suffix = uuid.uuid4().hex[:8]
    vpc_name = f"isv-sg-scoping-{args.scope}-{suffix}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": f"sg_{args.scope}_scoping",
        "scope": args.scope,
        "tests": {},
    }

    vpc_id = None
    try:
        vpc_result = create_test_vpc(ec2, CIDR, vpc_name)
        if not vpc_result["passed"]:
            result["tests"]["create_sg"] = {"passed": False, "error": "VPC creation failed"}
            print(json.dumps(result, indent=2))
            return 1

        vpc_id = vpc_result["vpc_id"]
        az = _get_az(ec2, args.region)

        if args.scope in ("workload", "node"):
            result["tests"] = test_workload_or_node_scoping(ec2, vpc_id, az, args.scope)
        elif args.scope == "service":
            result["tests"] = test_service_scoping(ec2, vpc_id, az, args.region)
        else:
            result["tests"] = test_subnet_scoping(ec2, vpc_id, az)

        result["success"] = all(t.get("passed") for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
    finally:
        if vpc_id:
            cleanup_vpc_resources(ec2, vpc_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
