#!/usr/bin/env python3
"""One-shot cleanup of stale network test resources in zCompute.

Deletes all VPCs, instances, EIPs, subnets, SGs, route tables, and IGWs
tagged CreatedBy=isvtest that were left behind by failed test runs.

Usage:
    cd isvctl/configs/providers/zcompute/config
    python3 ../scripts/network/cleanup_stale_resources.py
"""

import json
import os
import sys
import time
import ssl
import warnings

ssl._create_default_https_context = ssl._create_unverified_context
warnings.filterwarnings("ignore")
try:
    import urllib3; urllib3.disable_warnings()
except Exception:
    pass

import botocore.httpsession as _bhs
_orig_init = _bhs.URLLib3Session.__init__
def _ssl_patched(self, *args, **kwargs):
    kwargs['verify'] = False
    _orig_init(self, *args, **kwargs)
_bhs.URLLib3Session.__init__ = _ssl_patched

import boto3
from botocore.exceptions import ClientError

ec2 = boto3.client(
    "ec2",
    region_name=os.environ.get("AWS_REGION", "symphony"),
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL_EC2"),
)

# zCompute ignores tag Filters server-side — fetch all and filter in Python
ISV_TAG_FILTER = [{"Name": "tag:CreatedBy", "Values": ["isvtest"]}]  # kept for reference


def tag_match(tags):
    return any(t.get("Key") == "CreatedBy" and t.get("Value") == "isvtest"
               for t in (tags or []))


def safe_delete(fn, desc, **kwargs):
    try:
        fn(**kwargs)
        print(f"  deleted {desc}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if "NotFound" in code or "InvalidState" in code:
            print(f"  already gone: {desc}")
        else:
            print(f"  WARN {desc}: {e}")


# ── 1. Terminate tagged instances ─────────────────────────────────────────────
print("\n=== Instances ===")
# zCompute ignores tag filters — describe all, post-filter by tag in Python
resp = ec2.describe_instances()
instance_ids = [
    i["InstanceId"]
    for r in resp.get("Reservations", [])
    for i in r.get("Instances", [])
    if tag_match(i.get("Tags", []))
    and i["State"]["Name"] not in ("terminated", "shutting-down")
]
if instance_ids:
    print(f"Terminating {len(instance_ids)} instance(s): {instance_ids}")
    ec2.terminate_instances(InstanceIds=instance_ids)
    print("  waiting for termination...")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        time.sleep(10)
        r2 = ec2.describe_instances(InstanceIds=instance_ids)
        states = [i["State"]["Name"] for res in r2["Reservations"] for i in res["Instances"]]
        if all(s in ("terminated", "shutting-down") for s in states):
            break
    print("  done")
else:
    print("  none found")

# ── 2. Release unassociated tagged EIPs ────────────────────────────────────────
print("\n=== EIPs ===")
try:
    eip_resp = ec2.describe_addresses()
    released = 0
    for eip in eip_resp.get("Addresses", []):
        if eip.get("AssociationId"):
            continue
        if tag_match(eip.get("Tags", [])):
            safe_delete(ec2.release_address, f"EIP {eip['AllocationId']}",
                        AllocationId=eip["AllocationId"])
            released += 1
    if released == 0:
        print("  none found")
except Exception as e:
    print(f"  WARN: {e}")

# ── 3. Delete tagged VPCs and all their resources ─────────────────────────────
print("\n=== VPCs ===")
# zCompute ignores tag filters — describe all, post-filter by tag in Python
all_vpcs = ec2.describe_vpcs().get("Vpcs", [])
vpcs = [v for v in all_vpcs if tag_match(v.get("Tags", []))]
print(f"Found {len(vpcs)} tagged VPC(s)")

for vpc in vpcs:
    vpc_id = vpc["VpcId"]
    print(f"\n  VPC {vpc_id} ({vpc.get('CidrBlock')})")

    # SGs
    for sg in ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("SecurityGroups", []):
        if sg["GroupName"] == "default":
            continue
        safe_delete(ec2.delete_security_group, f"SG {sg['GroupId']}", GroupId=sg["GroupId"])

    # Subnets
    for sn in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("Subnets", []):
        safe_delete(ec2.delete_subnet, f"subnet {sn['SubnetId']}", SubnetId=sn["SubnetId"])

    # Route tables (non-main)
    for rtb in ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("RouteTables", []):
        if any(a.get("Main") for a in rtb.get("Associations", [])):
            continue
        for assoc in rtb.get("Associations", []):
            if not assoc.get("Main") and assoc.get("RouteTableAssociationId"):
                try:
                    ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
                except Exception:
                    pass
        safe_delete(ec2.delete_route_table, f"RTB {rtb['RouteTableId']}", RouteTableId=rtb["RouteTableId"])

    # IGWs
    for igw in ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]).get("InternetGateways", []):
        try:
            ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
        except Exception:
            pass
        safe_delete(ec2.delete_internet_gateway, f"IGW {igw['InternetGatewayId']}",
                    InternetGatewayId=igw["InternetGatewayId"])

    # VPC
    safe_delete(ec2.delete_vpc, f"VPC {vpc_id}", VpcId=vpc_id)

print("\n=== Done ===")
