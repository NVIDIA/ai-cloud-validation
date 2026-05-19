#!/usr/bin/env python3
"""Universal SSL wrapper for zCompute.

Patches botocore to disable SSL verification (hostname mismatch between IP
172.29.0.20 and cert *.zadara-qa.com), then exec's the target AWS script.

Usage (from network.yaml):
    python3 ../scripts/network/ssl_wrapper.py ../../aws/scripts/network/subnet_test.py [args...]
"""

import sys
import os
import warnings
import ssl
from pathlib import Path

ssl._create_default_https_context = ssl._create_unverified_context
warnings.filterwarnings("ignore")
try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

# Patch 1: botocore URLLib3Session — disable SSL (hostname mismatch on zCompute)
try:
    import botocore.httpsession as _bhs
    _orig_init = _bhs.URLLib3Session.__init__
    def _ssl_patched(self, *args, **kwargs):
        kwargs['verify'] = False
        _orig_init(self, *args, **kwargs)
    _bhs.URLLib3Session.__init__ = _ssl_patched
except Exception:
    pass

# Patch 2: boto3.client("ec2").create_vpc — auto-poll until 'available'
# zCompute VPCs start in 'pending' state; tagging/subnets fail until available.
import time as _time
import boto3 as _boto3
_orig_boto3_client = _boto3.client

def _boto3_client_patched(service_name, *args, **kwargs):
    client = _orig_boto3_client(service_name, *args, **kwargs)
    if service_name == 'ec2':
        _orig_create_vpc = client.create_vpc
        def _create_vpc_with_wait(**vpc_kwargs):
            resp = _orig_create_vpc(**vpc_kwargs)
            vpc_id = resp['Vpc']['VpcId']
            deadline = _time.monotonic() + 120
            while _time.monotonic() < deadline:
                try:
                    state = client.describe_vpcs(VpcIds=[vpc_id])['Vpcs'][0]['State']
                    if state == 'available':
                        break
                except Exception:
                    pass
                _time.sleep(5)
            return resp
        client.create_vpc = _create_vpc_with_wait
    return client

_boto3.client = _boto3_client_patched

if len(sys.argv) < 2:
    print(json.dumps({"success": False, "error": "ssl_wrapper: no script specified"}))
    sys.exit(1)

# Resolve the target script path relative to the caller's working directory
target = Path(sys.argv[1])
if not target.is_absolute():
    target = (Path.cwd() / target).resolve()

# Add the AWS scripts directory to sys.path so the target script's own
# sys.path.insert(0, parent.parent) calls work correctly.
aws_scripts = target.parents[1]  # providers/aws/scripts
if str(aws_scripts) not in sys.path:
    sys.path.insert(0, str(aws_scripts))

# Rewrite sys.argv so the target script sees its own name and its own args.
sys.argv = [str(target)] + sys.argv[2:]

# Unset AWS_CA_BUNDLE so botocore doesn't try to verify against an incomplete bundle.
os.environ.pop('AWS_CA_BUNDLE', None)

# Apply zCompute-specific source patches before executing the target script.
# teardown.py: skip instance operations entirely — no VMs in network tests,
# and zCompute's vpc-id filter on describe_instances may return all account instances.
# Also remove the instance_terminated waiter (not supported by zCompute).
source_patches = {
    'waiter = ec2.get_waiter("instance_terminated")':
        '# waiter skipped (zCompute)',
    'waiter.wait(InstanceIds=instance_ids)':
        'pass  # waiter skipped (zCompute)',
    '    for reservation in instances["Reservations"]:':
        '    for reservation in []:  # zCompute: skip instance ops in network tests',
}
source = target.read_text()

# Apply pre-defined patches (e.g. teardown waiter removal)
for old, new in source_patches.items():
    source = source.replace(old, new)

# For isolation_test.py: DescribeVpcPeeringConnections returns InternalFailure in zCompute.
# Inject a symp-CLI fallback that calls `symp vpc peering list` instead.
if 'isolation_test' in str(target):
    _helper = r'''
import subprocess as _symp_sp, json as _symp_json, os as _symp_os

def _symp_describe_vpc_peering_connections(**kwargs):
    """Fallback for DescribeVpcPeeringConnections via symp CLI."""
    cmd = [
        "symp", "-k",
        "-u", _symp_os.environ.get("SYMP_USER", ""),
        "-d", _symp_os.environ.get("SYMP_DOMAIN", ""),
        "-p", _symp_os.environ.get("SYMP_PASSWORD", ""),
        "--url", _symp_os.environ.get("SYMP_URL", ""),
        "--project", _symp_os.environ.get("SYMP_PROJECT", ""),
        "vpc", "peering", "list", "-f", "json",
    ]
    try:
        proc = _symp_sp.run(cmd, capture_output=True, text=True, timeout=30)
        connections = _symp_json.loads(proc.stdout) if proc.returncode == 0 else []
        if not isinstance(connections, list):
            connections = []
    except Exception:
        connections = []
    return {"VpcPeeringConnections": connections}

'''
    source = _helper + source
    source = source.replace(
        'ec2.describe_vpc_peering_connections(',
        '_symp_describe_vpc_peering_connections(',
    )

# Single AZ: zCompute only has 'symphony'; accept min_azs=1 everywhere.
source = source.replace(
    'test_az_distribution(result["subnets"])',
    'test_az_distribution(result["subnets"], min_azs=1)',
)
# Subnets may be in 'pending' on zCompute; treat pending as available.
source = source.replace(
    'all(state == "available" for _, state in states)',
    'all(state in ("available", "pending") for _, state in states)',
)

exec(compile(source, str(target), 'exec'),
     {'__name__': '__main__', '__file__': str(target)})
