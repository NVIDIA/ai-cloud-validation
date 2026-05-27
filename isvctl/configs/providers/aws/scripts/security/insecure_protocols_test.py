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

"""Verify insecure protocols disabled on AWS-provisioned edge endpoint (SEC13-02).

Self-contained AWS reference. Provisions a controlled internet-facing ALB
edge endpoint backed by AWS's modern TLS security policy, probes it with
the shared raw-socket prober, asserts each legacy protocol is refused,
then tears the fixture down in a finally block:

  VPC + IGW + two public subnets + route table + SG
  IAM server certificate (self-signed RSA-2048)
  Internet-facing ALB with HTTPS fixed-response listener (ELBSecurityPolicy-TLS13-1-2-2021-06)

Per-fixture resources share an ``isv-sec13-test-<suffix>`` prefix and the
``CreatedBy=isvtest`` tag so the security teardown sweep can clean up
anything this script leaks on a hard crash.

When ``EDGE_ENDPOINTS`` (or ``--endpoints``) is supplied, fixture
provisioning is skipped and the operator-supplied edge endpoints are
probed directly. This is the escape hatch for tenants who want to
verify their real edge surface instead of the provider-capability
fixture.

Usage:
    python insecure_protocols_test.py --region us-west-2
    EDGE_ENDPOINTS=host:443 python insecure_protocols_test.py --region us-west-2

Output JSON: matches the SEC13-02 contract emitted by the shared probe.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from common.errors import TRANSIENT_AWS_CODES, classify_aws_error, delete_with_retry, handle_aws_errors
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

TEST_NAME = "insecure_protocols"
FIXTURE_PREFIX = "isv-sec13-test-"
VPC_CIDR = "10.31.0.0/24"
SUBNET_CIDRS = ("10.31.0.0/25", "10.31.0.128/25")
TLS_SECURITY_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"

# AWS error codes from setup that signal the orchestrator principal cannot
# provision the test fixture; surface as a structured skip rather than a
# SEC13-02 failure.
SKIPPABLE_SETUP_ERRORS = frozenset({"AccessDenied", "UnauthorizedOperation"})
CERT_DELETE_TRANSIENT_CODES = TRANSIENT_AWS_CODES | frozenset({"DeleteConflict"})
LB_RESOURCE_RELEASE_TRANSIENT_CODES = TRANSIENT_AWS_CODES | frozenset({"DependencyViolation"})
LB_RESOURCE_RELEASE_RETRY_ATTEMPTS = 10
LB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS = 3.0
# IAM's "cert in use" view lags load balancer deletion by several minutes even after
# all ENIs have drained, so cert delete needs a larger budget than the
# VPC-side waits. 15 attempts * 3s linear backoff = ~5.25 min total wait.
IAM_CERT_DELETE_RETRY_ATTEMPTS = 15
IAM_CERT_DELETE_RETRY_BACKOFF_SECONDS = 3.0

# ELB ENIs may not accept TCP for a few seconds after the LB reports
# ``active``. Retry the probe a handful of times before declaring failure
# so a startup race doesn't flag a healthy fixture as broken.
LB_PROBE_RETRY_ATTEMPTS = 6
LB_PROBE_RETRY_BACKOFF = 5.0


def _parse_tcp_port_arg(value: str) -> int:
    """Parse an argparse TCP port value."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--http-port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--http-port must be 1-65535")
    return port


def _bad_input_result(error: str) -> dict[str, Any]:
    """Return a structured bad-input payload."""
    return {
        "success": False,
        "platform": "security",
        "test_name": TEST_NAME,
        "error": error,
        "error_type": "bad_input",
    }


def _provider_error_message(error_type: str) -> str:
    """Return a provider-neutral top-level message for classified provider failures."""
    messages = {
        "credentials_missing": "provider credentials not found",
        "credentials_expired": "provider credentials expired",
        "credentials_invalid": "provider credentials invalid",
        "profile_not_found": "provider profile not found",
        "access_denied": "provider access denied",
    }
    return messages.get(error_type, "provider operation failed")


def _resource_delete_failed(resource: str, *, action: str = "delete") -> str:
    """Return a provider-neutral cleanup failure message."""
    return f"resource_delete_failed: failed to {action} {resource}"


class FixtureReachabilityError(RuntimeError):
    """Raised when the load balancer fixture never reaches TCP/TLS readiness."""


def _load_shared_probe() -> Any:
    """Load providers/shared/insecure_protocols_test.py as a module."""
    shared_path = Path(__file__).resolve().parents[3] / "shared" / "insecure_protocols_test.py"
    spec = importlib.util.spec_from_file_location("shared_insecure_protocols_probe", shared_path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load shared probe at {shared_path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class Fixture:
    """Resources created during setup, used during probe and teardown."""

    suffix: str
    vpc_id: str = ""
    igw_id: str = ""
    subnet_id: str = ""
    subnet_ids: list[str] = field(default_factory=list)
    route_table_id: str = ""
    route_table_assoc_id: str = ""
    route_table_assoc_ids: list[str] = field(default_factory=list)
    sg_id: str = ""
    cert_arn: str = ""
    cert_name: str = ""
    target_group_arn: str = ""
    load_balancer_arn: str = ""
    load_balancer_dns: str = ""
    listener_arn: str = ""
    created: dict[str, bool] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Return the canonical ``isv-sec13-test-<suffix>`` identifier."""
        return f"{FIXTURE_PREFIX}{self.suffix}"


def _isvtest_tags(name: str) -> list[dict[str, str]]:
    """Standard ``CreatedBy=isvtest`` + ``Name`` tag pair for EC2 resources."""
    return [
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "Name", "Value": name},
    ]


def _elbv2_tags(name: str) -> list[dict[str, str]]:
    """Same shape as EC2 tags - ELBv2 accepts the EC2-style Key/Value list."""
    return _isvtest_tags(name)


def _create_vpc(ec2: Any, fixture: Fixture) -> None:
    """Create VPC + IGW + public subnets + route table + SG; populate fixture in place."""
    vpc = ec2.create_vpc(CidrBlock=VPC_CIDR)
    fixture.vpc_id = vpc["Vpc"]["VpcId"]
    fixture.created["vpc"] = True
    ec2.create_tags(Resources=[fixture.vpc_id], Tags=_isvtest_tags(fixture.name))
    ec2.get_waiter("vpc_available").wait(VpcIds=[fixture.vpc_id])

    igw = ec2.create_internet_gateway()
    fixture.igw_id = igw["InternetGateway"]["InternetGatewayId"]
    fixture.created["igw"] = True
    ec2.create_tags(Resources=[fixture.igw_id], Tags=_isvtest_tags(fixture.name))
    ec2.attach_internet_gateway(InternetGatewayId=fixture.igw_id, VpcId=fixture.vpc_id)
    fixture.created["igw_attached"] = True

    azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"]
    zone_names = [az["ZoneName"] for az in azs if az.get("ZoneName")]
    if len(zone_names) < len(SUBNET_CIDRS):
        msg = f"SEC13-02 ALB fixture needs {len(SUBNET_CIDRS)} availability zones, found {len(zone_names)}"
        raise RuntimeError(msg)

    for cidr, zone_name in zip(SUBNET_CIDRS, zone_names[: len(SUBNET_CIDRS)], strict=True):
        subnet = ec2.create_subnet(VpcId=fixture.vpc_id, CidrBlock=cidr, AvailabilityZone=zone_name)
        subnet_id = subnet["Subnet"]["SubnetId"]
        fixture.subnet_ids.append(subnet_id)
        if not fixture.subnet_id:
            fixture.subnet_id = subnet_id
        fixture.created["subnet"] = True
        ec2.create_tags(Resources=[subnet_id], Tags=_isvtest_tags(fixture.name))

    rt = ec2.create_route_table(VpcId=fixture.vpc_id)
    fixture.route_table_id = rt["RouteTable"]["RouteTableId"]
    fixture.created["route_table"] = True
    ec2.create_tags(Resources=[fixture.route_table_id], Tags=_isvtest_tags(fixture.name))
    ec2.create_route(
        RouteTableId=fixture.route_table_id,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=fixture.igw_id,
    )
    for subnet_id in fixture.subnet_ids:
        assoc = ec2.associate_route_table(RouteTableId=fixture.route_table_id, SubnetId=subnet_id)
        assoc_id = assoc["AssociationId"]
        fixture.route_table_assoc_ids.append(assoc_id)
        if not fixture.route_table_assoc_id:
            fixture.route_table_assoc_id = assoc_id
        fixture.created["route_table_assoc"] = True

    sg = ec2.create_security_group(
        GroupName=f"{fixture.name}-sg",
        Description=f"SEC13-02 ALB edge fixture SG for {fixture.name}",
        VpcId=fixture.vpc_id,
    )
    fixture.sg_id = sg["GroupId"]
    fixture.created["sg"] = True
    ec2.create_tags(Resources=[fixture.sg_id], Tags=_isvtest_tags(fixture.name))
    ec2.authorize_security_group_ingress(
        GroupId=fixture.sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SEC13-02 TLS probe ingress"}],
            }
        ],
    )


def _generate_self_signed_cert(common_name: str) -> tuple[str, str]:
    """Generate a self-signed RSA-2048 cert. Returns (cert_pem, key_pem).

    The probe never validates the cert (it inspects the ServerHello version
    field only), so a CN mismatch with the load balancer DNS name is fine.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


def _upload_iam_server_cert(iam: Any, fixture: Fixture) -> None:
    """Generate a self-signed cert and upload it as an IAM server certificate."""
    cert_pem, key_pem = _generate_self_signed_cert(common_name=fixture.name)
    fixture.cert_name = fixture.name
    response = iam.upload_server_certificate(
        ServerCertificateName=fixture.cert_name,
        CertificateBody=cert_pem,
        PrivateKey=key_pem,
        Tags=[{"Key": "CreatedBy", "Value": "isvtest"}, {"Key": "Name", "Value": fixture.name}],
    )
    fixture.cert_arn = response["ServerCertificateMetadata"]["Arn"]
    fixture.created["iam_server_cert"] = True


def _create_load_balancer(elbv2: Any, fixture: Fixture) -> None:
    """Create the internet-facing ALB and wait for it to become active."""
    response = elbv2.create_load_balancer(
        Name=f"{fixture.name}-alb"[:32],
        Subnets=fixture.subnet_ids,
        Scheme="internet-facing",
        Type="application",
        SecurityGroups=[fixture.sg_id],
        IpAddressType="ipv4",
        Tags=_elbv2_tags(fixture.name),
    )
    lb = response["LoadBalancers"][0]
    fixture.load_balancer_arn = lb["LoadBalancerArn"]
    fixture.load_balancer_dns = lb["DNSName"]
    fixture.created["load_balancer"] = True
    elbv2.get_waiter("load_balancer_available").wait(
        LoadBalancerArns=[fixture.load_balancer_arn],
        WaiterConfig={"Delay": 15, "MaxAttempts": 40},
    )


def _create_tls_listener(elbv2: Any, fixture: Fixture) -> None:
    """Bind the HTTPS listener with the modern security policy + self-signed cert.

    Pinning ``ELBSecurityPolicy-TLS13-1-2-2021-06`` is what makes the
    fixture meaningful: AWS documents this policy as refusing TLS < 1.2.
    """
    response = elbv2.create_listener(
        LoadBalancerArn=fixture.load_balancer_arn,
        Protocol="HTTPS",
        Port=443,
        SslPolicy=TLS_SECURITY_POLICY,
        Certificates=[{"CertificateArn": fixture.cert_arn}],
        DefaultActions=[
            {
                "Type": "fixed-response",
                "FixedResponseConfig": {
                    "StatusCode": "200",
                    "ContentType": "text/plain",
                    "MessageBody": "SEC13-02 probe endpoint",
                },
            }
        ],
        Tags=_elbv2_tags(fixture.name),
    )
    fixture.listener_arn = response["Listeners"][0]["ListenerArn"]
    fixture.created["listener"] = True


def _provision_fixture(*, ec2: Any, iam: Any, elbv2: Any, fixture: Fixture) -> None:
    """Provision the SEC13-02 edge fixture; mutates fixture in place."""
    _create_vpc(ec2, fixture)
    _upload_iam_server_cert(iam, fixture)
    _create_load_balancer(elbv2, fixture)
    _create_tls_listener(elbv2, fixture)


def _detach_internet_gateway(ec2: Any, *, internet_gateway_id: str, vpc_id: str) -> None:
    """Detach an IGW, treating already-detached/not-found as successful cleanup."""
    try:
        ec2.detach_internet_gateway(InternetGatewayId=internet_gateway_id, VpcId=vpc_id)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"Gateway.NotAttached", "InvalidInternetGatewayID.NotFound"}:
            return
        raise


def _teardown_fixture(*, ec2: Any, iam: Any, elbv2: Any, fixture: Fixture) -> list[str]:
    """Best-effort teardown of every resource the setup helpers created."""
    errors: list[str] = []

    if fixture.created.get("listener") and fixture.listener_arn:
        try:
            elbv2.delete_listener(ListenerArn=fixture.listener_arn)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ListenerNotFound":
                errors.append(_resource_delete_failed("listener"))

    if fixture.created.get("load_balancer") and fixture.load_balancer_arn:
        try:
            elbv2.delete_load_balancer(LoadBalancerArn=fixture.load_balancer_arn)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "LoadBalancerNotFound":
                errors.append(_resource_delete_failed("load balancer"))
        else:
            try:
                elbv2.get_waiter("load_balancers_deleted").wait(
                    LoadBalancerArns=[fixture.load_balancer_arn],
                    WaiterConfig={"Delay": 10, "MaxAttempts": 30},
                )
            except (ClientError, WaiterError):
                errors.append(_resource_delete_failed("load balancer", action="wait for deletion of"))

    if fixture.created.get("target_group") and fixture.target_group_arn:
        if not delete_with_retry(
            elbv2.delete_target_group,
            TargetGroupArn=fixture.target_group_arn,
            resource_desc="target group",
        ):
            errors.append(_resource_delete_failed("target group"))

    assoc_ids = fixture.route_table_assoc_ids or (
        [fixture.route_table_assoc_id] if fixture.route_table_assoc_id else []
    )
    if fixture.created.get("route_table_assoc"):
        for assoc_id in assoc_ids:
            try:
                ec2.disassociate_route_table(AssociationId=assoc_id)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "InvalidAssociationID.NotFound":
                    errors.append(_resource_delete_failed("route table association", action="disassociate"))

    if fixture.created.get("route_table") and fixture.route_table_id:
        if not delete_with_retry(
            ec2.delete_route_table,
            RouteTableId=fixture.route_table_id,
            resource_desc="route table",
        ):
            errors.append(_resource_delete_failed("route table"))

    if fixture.created.get("sg") and fixture.sg_id:
        if not delete_with_retry(
            ec2.delete_security_group,
            GroupId=fixture.sg_id,
            resource_desc="security group",
            attempts=LB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
            backoff_seconds=LB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
            transient_codes=LB_RESOURCE_RELEASE_TRANSIENT_CODES,
        ):
            errors.append(_resource_delete_failed("security group"))

    subnet_ids = fixture.subnet_ids or ([fixture.subnet_id] if fixture.subnet_id else [])
    if fixture.created.get("subnet"):
        for subnet_id in subnet_ids:
            if not delete_with_retry(
                ec2.delete_subnet,
                SubnetId=subnet_id,
                resource_desc="subnet",
                attempts=LB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
                backoff_seconds=LB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
                transient_codes=LB_RESOURCE_RELEASE_TRANSIENT_CODES,
            ):
                errors.append(_resource_delete_failed("subnet"))

    if fixture.created.get("igw_attached") and fixture.igw_id and fixture.vpc_id:
        if not delete_with_retry(
            _detach_internet_gateway,
            ec2,
            internet_gateway_id=fixture.igw_id,
            vpc_id=fixture.vpc_id,
            resource_desc="internet gateway attachment",
            attempts=LB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
            backoff_seconds=LB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
            transient_codes=LB_RESOURCE_RELEASE_TRANSIENT_CODES,
        ):
            errors.append(_resource_delete_failed("internet gateway", action="detach"))

    if fixture.created.get("igw") and fixture.igw_id:
        if not delete_with_retry(
            ec2.delete_internet_gateway,
            InternetGatewayId=fixture.igw_id,
            resource_desc="internet gateway",
            attempts=LB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
            backoff_seconds=LB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
            transient_codes=LB_RESOURCE_RELEASE_TRANSIENT_CODES,
        ):
            errors.append(_resource_delete_failed("internet gateway"))

    if fixture.created.get("vpc") and fixture.vpc_id:
        if not delete_with_retry(
            ec2.delete_vpc,
            VpcId=fixture.vpc_id,
            resource_desc="virtual network",
            attempts=LB_RESOURCE_RELEASE_RETRY_ATTEMPTS,
            backoff_seconds=LB_RESOURCE_RELEASE_RETRY_BACKOFF_SECONDS,
            transient_codes=LB_RESOURCE_RELEASE_TRANSIENT_CODES,
        ):
            errors.append(_resource_delete_failed("virtual network"))

    if fixture.created.get("iam_server_cert") and fixture.cert_name:
        # Load balancer deletion releases the cert reference asynchronously; run this
        # after VPC-side dependency cleanup so those waits can absorb the
        # longer ENI/public-IP drain window before IAM sees the cert unused.
        if not delete_with_retry(
            iam.delete_server_certificate,
            ServerCertificateName=fixture.cert_name,
            resource_desc="server certificate",
            attempts=IAM_CERT_DELETE_RETRY_ATTEMPTS,
            backoff_seconds=IAM_CERT_DELETE_RETRY_BACKOFF_SECONDS,
            transient_codes=CERT_DELETE_TRANSIENT_CODES,
        ):
            errors.append(_resource_delete_failed("server certificate"))

    return errors


def _probe_endpoints(
    probe: Any,
    endpoints: list[tuple[str, int]],
    http_port: int,
    timeout: float,
) -> dict[str, Any]:
    """Build the SEC13-02 contract dict from probes against ``endpoints``."""
    tests = probe._aggregate(endpoints, http_port=http_port, timeout=timeout)
    return {
        "success": all(tests[name]["passed"] for name in probe.REQUIRED_TESTS),
        "platform": "security",
        "test_name": TEST_NAME,
        "endpoints_tested": len(endpoints),
        "tests": tests,
    }


def _wait_for_load_balancer_reachable(probe: Any, host: str, timeout: float) -> None:
    """Send a TLSv1.2 probe until the load balancer returns ServerHello (or give up).

    ``LoadBalancerAvailable`` reports ``active`` slightly before the ELB
    ENIs accept TCP; without this poll the first real probe can spuriously
    time out and flag a healthy fixture as broken.
    """
    last_result: dict[str, Any] | None = None
    for attempt in range(LB_PROBE_RETRY_ATTEMPTS):
        result = probe.probe_tls_version(host, 443, 0x0303, timeout=timeout)
        last_result = result
        if result.get("category") in {"accepted", "refused"} or str(result.get("category", "")).startswith(
            "downgraded:"
        ):
            return
        if attempt < LB_PROBE_RETRY_ATTEMPTS - 1:
            time.sleep(LB_PROBE_RETRY_BACKOFF)
    last = json.dumps(last_result, sort_keys=True, default=str)
    raise FixtureReachabilityError(
        f"load balancer fixture {host}:443 never became reachable after "
        f"{LB_PROBE_RETRY_ATTEMPTS} TLS probes; last_result={last}"
    )


def _skipped_result(reason: str) -> dict[str, Any]:
    """Return a structured top-level skip payload."""
    return {
        "success": True,
        "platform": "security",
        "test_name": TEST_NAME,
        "skipped": True,
        "skip_reason": reason,
    }


def _parse_override_endpoints(probe: Any, spec: str) -> list[tuple[str, int]] | None:
    """Parse ``--endpoints``/``EDGE_ENDPOINTS`` if supplied; return None when empty."""
    spec = spec.strip()
    if not spec:
        return None
    return probe._parse_endpoints(spec)


@handle_aws_errors
def main() -> int:
    """Provision the SEC13-02 ALB fixture, probe, emit JSON, clean up."""
    parser = argparse.ArgumentParser(description="SEC13-02 insecure-protocols test (AWS)", exit_on_error=False)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--endpoints",
        default=os.environ.get("EDGE_ENDPOINTS", ""),
        help="Comma-separated host:port list; when set, skip fixture and probe these directly",
    )
    parser.add_argument(
        "--http-port",
        type=_parse_tcp_port_arg,
        default=os.environ.get("EDGE_HTTP_PORT", "80"),
        help="Port to probe for plain HTTP (default 80)",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-probe socket timeout in seconds")
    try:
        args = parser.parse_args()
    except argparse.ArgumentError as exc:
        print(json.dumps(_bad_input_result(str(exc)), indent=2))
        return 1

    probe = _load_shared_probe()

    if probe.DEMO_MODE:
        demo = probe._demo_result()
        print(json.dumps(demo, indent=2))
        return 0

    try:
        override = _parse_override_endpoints(probe, args.endpoints)
    except ValueError as exc:
        print(json.dumps(_bad_input_result(str(exc)), indent=2))
        return 1

    if override is not None:
        result = _probe_endpoints(probe, override, http_port=args.http_port, timeout=args.timeout)
        print(json.dumps(result, indent=2))
        return 0 if result["success"] else 1

    region = args.region
    ec2 = boto3.client("ec2", region_name=region)
    iam = boto3.client("iam", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)

    fixture = Fixture(suffix=uuid.uuid4().hex[:8])
    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": TEST_NAME,
    }
    skip_payload: dict[str, Any] | None = None

    try:
        try:
            _provision_fixture(ec2=ec2, iam=iam, elbv2=elbv2, fixture=fixture)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in SKIPPABLE_SETUP_ERRORS:
                skip_payload = _skipped_result(
                    "cannot provision SEC13-02 edge fixture: missing required setup permissions"
                )
            else:
                raise

        if skip_payload is None:
            _wait_for_load_balancer_reachable(probe, fixture.load_balancer_dns, timeout=args.timeout)
            endpoints = [(fixture.load_balancer_dns, 443)]
            result = _probe_endpoints(probe, endpoints, http_port=args.http_port, timeout=args.timeout)
    except FixtureReachabilityError as exc:
        result["error"] = str(exc)
        result["error_type"] = "fixture_unreachable"
        result["success"] = False
    except (ClientError, WaiterError, BotoCoreError) as exc:
        error_type, _ = classify_aws_error(exc)
        result["error_type"] = error_type
        result["error"] = _provider_error_message(error_type)
        result["success"] = False
    finally:
        cleanup_errors: list[str] = []
        try:
            cleanup_errors = _teardown_fixture(ec2=ec2, iam=iam, elbv2=elbv2, fixture=fixture)
        except (ClientError, WaiterError, BotoCoreError):
            cleanup_errors.append("resource_delete_failed: unexpected cleanup failure")
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            existing = result.get("error")
            msg = "cleanup failed"
            result.setdefault("error_type", "cleanup_failed")
            result["error"] = f"{existing}; {msg}" if existing else msg
            result["success"] = False

    if skip_payload is not None and not result.get("cleanup_errors"):
        print(json.dumps(skip_payload, indent=2))
        return 0

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
