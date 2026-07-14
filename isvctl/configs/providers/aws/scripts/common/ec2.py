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

"""Shared EC2 helper utilities.

Provides common EC2 operations used across VM and ISO launch scripts:
- Key pair creation with idempotent handling
- Key-name sanitization (prevents path traversal)
- Security group creation with SSH ingress
- Availability zone support detection
- Default VPC and subnet discovery
- Post-transition public-IP polling (no stale-IP fallback)
"""

from __future__ import annotations

import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from common.ebs import describe_instance
from common.errors import TRANSIENT_AWS_CODES

# Conservative key-name pattern - letters, digits, dash, underscore, dot.
# Deliberately excludes '/' and '..' to prevent path traversal when the
# name is composed into a filesystem path like /tmp/{key_name}.pem
# Length cap matches EC2's 255-char limit.
_KEY_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,255}")


def sanitize_key_name(key_name: str) -> str:
    """Validate ``key_name`` is safe to compose into a filesystem path.

    AWS key pair names are a CLI-supplied string that several stubs
    concatenate into ``/tmp/{key_name}.pem`` (and similar). A name like
    ``../etc/passwd`` or ``foo/bar`` would escape ``/tmp`` and read or
    unlink files outside the intended scope. This is low-severity on a
    dedicated test box but real on a dev machine or shared runner.

    Reject anything outside ``[A-Za-z0-9_.-]`` with a clear error. Returns
    ``key_name`` unchanged on success so callers can use it inline:

        pem_path = Path(f"/tmp/{sanitize_key_name(args.key_name)}.pem")

    Args:
        key_name: The proposed key-pair name.

    Returns:
        The same ``key_name`` if valid.

    Raises:
        ValueError: If ``key_name`` contains characters outside the
            allowed set or is empty / too long.
    """
    if not key_name or not _KEY_NAME_RE.fullmatch(key_name):
        raise ValueError(
            f"invalid key name {key_name!r}: must match [A-Za-z0-9_.-] "
            "(1-255 chars). Rejected to prevent path traversal when composed "
            "into /tmp/<name>.pem."
        )
    return key_name


def wait_for_public_ip(
    ec2: Any,
    instance_id: str,
    *,
    timeout: int = 120,
    interval: int = 5,
) -> str | None:
    """Poll ``describe_instances`` until the instance has a non-null public IP.

    AWS preserves the public IP across stop/start, so post-transition stubs
    historically fell back to the pre-stop IP passed on the CLI. On NCPs
    that release the ephemeral IP on stop (GCP is the most common), that
    fallback silently masks a stale IP. The defensive default is to poll
    the describe API and never trust a pre-stop value.

    Args:
        ec2: Boto3 EC2 client.
        instance_id: Instance to poll.
        timeout: Total seconds to wait before giving up.
        interval: Seconds between describe calls.

    Returns:
        The fresh public IP string, or None if the instance still has no
        public IP after ``timeout`` seconds (caller decides how to surface).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = ec2.describe_instances(InstanceIds=[instance_id])
            reservations = resp.get("Reservations", [])
            instances = reservations[0].get("Instances", []) if reservations else []
            public_ip = instances[0].get("PublicIpAddress") if instances else None
            if public_ip:
                return public_ip
        except ClientError as e:
            # Only swallow throttling / server-side transient codes. Terminal
            # errors (InvalidInstanceID.NotFound, AuthFailure, AccessDenied)
            # must surface - hiding them behind the timeout makes bad configs
            # look like slow IP assignment.
            code = e.response.get("Error", {}).get("Code", "")
            if code not in TRANSIENT_AWS_CODES:
                raise
            print(f"Warning: describe_instances transient error ({code}): {e}", file=sys.stderr)
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, ConnectionClosedError) as e:
            # Transport-level failures only - non-network BotoCoreErrors (e.g.
            # ParamValidationError, NoCredentialsError) should not be swallowed.
            print(f"Warning: describe_instances network error: {e}", file=sys.stderr)
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def instance_network(ec2: Any, instance_id: str) -> dict[str, str]:
    """Return network and SSH fields for an existing EC2 instance."""
    instance = describe_instance(ec2, instance_id)
    security_groups = instance.get("SecurityGroups") or []
    if not security_groups:
        raise RuntimeError(f"Instance {instance_id} has no security group")
    public_ip = instance.get("PublicIpAddress")
    private_ip = instance.get("PrivateIpAddress")
    subnet_id = instance.get("SubnetId")
    vpc_id = instance.get("VpcId")
    if not all((public_ip, private_ip, subnet_id, vpc_id)):
        raise RuntimeError(f"Instance {instance_id} is missing required network fields")
    return {
        "public_ip": str(public_ip),
        "private_ip": str(private_ip),
        "subnet_id": str(subnet_id),
        "vpc_id": str(vpc_id),
        "security_group_id": str(security_groups[0]["GroupId"]),
    }


def get_supported_azs(ec2: Any, instance_type: str) -> set[str]:
    """Get availability zones that support the given instance type.

    Args:
        ec2: Boto3 EC2 client.
        instance_type: EC2 instance type to check (e.g., 'g4dn.xlarge').

    Returns:
        Set of availability zone names, or empty set if the query fails.
    """
    try:
        response = ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )
        return {offering["Location"] for offering in response.get("InstanceTypeOfferings", [])}
    except ClientError as e:
        print(f"Warning: Could not get AZ offerings: {e}", file=sys.stderr)
        return set()


def candidate_availability_zones(ec2: Any, availability_zone: str, instance_type: str) -> list[str]:
    """Return the requested AZ, or all AZs offering the instance type when unpinned.

    A non-empty ``availability_zone`` pins the result to that single zone. An
    empty value falls back to every supported AZ (sorted) so callers can retry
    across zones after a capacity shortage.

    Args:
        ec2: Boto3 EC2 client.
        availability_zone: Requested AZ, or "" to auto-select supported zones.
        instance_type: EC2 instance type the AZ must offer.

    Returns:
        Ordered list of candidate availability zones.

    Raises:
        RuntimeError: If no AZ offers the instance type when none was pinned.
    """
    if availability_zone.strip():
        return [availability_zone.strip()]

    supported = get_supported_azs(ec2, instance_type)
    if not supported:
        raise RuntimeError(f"No AWS availability zones offer {instance_type}")
    return sorted(supported)


def get_default_vpc_and_subnets(
    ec2: Any,
    instance_type: str,
) -> tuple[str, list[str]]:
    """Get default VPC and subnets in AZs that support the instance type.

    Subnets in supported AZs are prioritized at the front of the list,
    with unsupported AZ subnets appended as fallbacks.

    Args:
        ec2: Boto3 EC2 client.
        instance_type: EC2 instance type (used to filter AZs).

    Returns:
        Tuple of (vpc_id, subnet_id_list).

    Raises:
        RuntimeError: If no default VPC or subnets are found.
    """
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])
    if not vpcs["Vpcs"]:
        raise RuntimeError("No default VPC found. Please specify --vpc-id and --subnet-id")

    vpc_id = vpcs["Vpcs"][0]["VpcId"]
    supported_azs = get_supported_azs(ec2, instance_type)

    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    if not subnets["Subnets"]:
        raise RuntimeError("No subnets found in default VPC")

    # Prioritize subnets in supported AZs
    subnet_list: list[str] = []
    for subnet in subnets["Subnets"]:
        az = subnet["AvailabilityZone"]
        subnet_id = subnet["SubnetId"]
        if not supported_azs or az in supported_azs:
            subnet_list.insert(0, subnet_id)
        else:
            subnet_list.append(subnet_id)

    if not subnet_list:
        raise RuntimeError("No subnets found in default VPC")

    return vpc_id, subnet_list


_ISV_CREATED_BY_TAG = {"Key": "CreatedBy", "Value": "isvtest"}


def _has_isv_tag(tags: list[dict[str, str]] | None) -> bool:
    """Return True if ``tags`` includes the ``CreatedBy=isvtest`` marker.

    Used as a verified-reuse signal: existing resources without this tag
    were created by something outside the suite and must not be adopted.
    """
    if not tags:
        return False
    return any(t.get("Key") == "CreatedBy" and t.get("Value") == "isvtest" for t in tags)


def create_key_pair(
    ec2: Any,
    key_name: str,
    key_dir: str | Path | None = None,
) -> str:
    """Create EC2 key pair and save the private key to a file.

    Reuse is explicit and verified: if a key pair with the same name already
    exists on AWS, the local PEM file must also exist and the AWS-side key
    must carry the suite's ``CreatedBy=isvtest`` tag. If the tag is missing
    the key belongs to some other caller and we raise rather than silently
    adopt it. If the local PEM is missing we recreate (the AWS-side key is
    useless without the private material and was ours to begin with).

    Args:
        ec2: Boto3 EC2 client.
        key_name: Name for the EC2 key pair.
        key_dir: Directory to store the .pem file.
            Defaults to /tmp.

    Returns:
        Path to the .pem key file.

    Raises:
        RuntimeError: If key pair creation fails, or if an existing key by
            the same name lacks the suite's ownership tag (verified-reuse
            check failed).
    """
    key_name = sanitize_key_name(key_name)

    if key_dir is None:
        key_dir = Path("/tmp")
    else:
        key_dir = Path(key_dir)

    key_path = key_dir / f"{key_name}.pem"

    # Check if key already exists - verify shape before reusing.
    try:
        describe = ec2.describe_key_pairs(KeyNames=[key_name])
        existing = describe.get("KeyPairs", [{}])[0]
        if not _has_isv_tag(existing.get("Tags")):
            raise RuntimeError(
                f"key pair {key_name!r} already exists on AWS but is not tagged "
                "CreatedBy=isvtest - refusing to adopt a resource this suite "
                "did not create. Either "
                "delete it manually or use a different --key-name."
            )
        # Tag matches - verified reuse. If we have the file locally, reuse it.
        if key_path.exists() and key_path.stat().st_size > 0:
            return str(key_path)
        # Tag matches but local PEM is missing/empty - ours but unrecoverable;
        # safe to delete and recreate.
        ec2.delete_key_pair(KeyName=key_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
            raise

    # Create new key pair
    try:
        response = ec2.create_key_pair(
            KeyName=key_name,
            TagSpecifications=[
                {
                    "ResourceType": "key-pair",
                    "Tags": [
                        {"Key": "Name", "Value": key_name},
                        _ISV_CREATED_BY_TAG,
                    ],
                }
            ],
        )
    except ClientError as e:
        raise RuntimeError(f"Failed to create key pair '{key_name}': {e}") from e

    key_dir.mkdir(parents=True, exist_ok=True)
    # Remove stale key file if it exists (PEM files are 0400/read-only)
    if key_path.exists():
        key_path.chmod(0o600)
        key_path.unlink()
    key_path.write_text(response["KeyMaterial"])
    key_path.chmod(0o400)
    print(f"Created key pair: {key_name}", file=sys.stderr)

    return str(key_path)


def _sg_has_ssh_rule(ip_permissions: list[dict[str, Any]] | None) -> bool:
    """Return True if the ingress rule set includes the expected SSH rule
    (tcp/22 from 0.0.0.0/0). Used as a shape check on reuse."""
    if not ip_permissions:
        return False
    for perm in ip_permissions:
        if (
            perm.get("IpProtocol") == "tcp"
            and perm.get("FromPort") == 22
            and perm.get("ToPort") == 22
            and any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", []))
        ):
            return True
    return False


def create_security_group(
    ec2: Any,
    vpc_id: str,
    name: str,
    description: str = "ISV validation security group",
) -> str:
    """Create a security group allowing SSH ingress, or return existing one.

    Reuse is explicit and verified: if a security group with the same name
    already exists in the VPC, we describe it and verify the invariants the
    caller expects - CreatedBy=isvtest tag, description match, and the
    required SSH ingress rule. If any differs, raise rather than silently
    adopt a resource whose shape may not match what the caller needs.

    Args:
        ec2: Boto3 EC2 client.
        vpc_id: VPC to create the security group in.
        name: Security group name.
        description: Security group description.

    Returns:
        Security group ID.

    Raises:
        RuntimeError: If an existing SG by the same name fails the
            verified-reuse checks (missing ownership tag, wrong description,
            or missing expected SSH ingress rule).
        ClientError: For AWS API errors other than duplicate group.
    """
    try:
        response = ec2.create_security_group(
            GroupName=name,
            Description=description,
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": name},
                        _ISV_CREATED_BY_TAG,
                    ],
                }
            ],
        )
        sg_id = response["GroupId"]

        # Allow SSH from anywhere (for testing)
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
                }
            ],
        )
        print(f"Created security group: {sg_id}", file=sys.stderr)
        return sg_id
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise

        sgs = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )
        if not sgs["SecurityGroups"]:
            # Duplicate error claims it exists but describe can't find it -
            # propagate the original error rather than silently swallow.
            raise

        existing = sgs["SecurityGroups"][0]
        sg_id = existing["GroupId"]

        # Verified-reuse checks - any mismatch raises rather than silently
        # adopting a resource whose shape we didn't enforce.
        if not _has_isv_tag(existing.get("Tags")):
            raise RuntimeError(
                f"security group {name!r} in VPC {vpc_id} already exists but is not tagged "
                "CreatedBy=isvtest - refusing to adopt a resource this suite did not "
                "create."
            )
        if existing.get("Description") != description:
            raise RuntimeError(
                f"security group {name!r} ({sg_id}) exists but description differs: "
                f"expected {description!r}, got {existing.get('Description')!r}"
            )
        if not _sg_has_ssh_rule(existing.get("IpPermissions")):
            raise RuntimeError(
                f"security group {name!r} ({sg_id}) exists but is missing the required "
                "SSH ingress rule (tcp/22 from 0.0.0.0/0) - refusing to reuse."
            )
        print(f"Reusing verified security group: {sg_id}", file=sys.stderr)
        return sg_id


def get_amazon_linux_ami(ec2: Any) -> str | None:
    """Get latest Amazon Linux 2 AMI (x86_64).

    Args:
        ec2: Boto3 EC2 client.

    Returns:
        AMI ID or None if not found.
    """
    try:
        response = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
        return images[0]["ImageId"] if images else None
    except ClientError as e:
        print(f"Warning: Could not get Amazon Linux AMI: {e}", file=sys.stderr)
        return None


def get_ubuntu_ami(ec2: Any, instance_type: str) -> str | None:
    """Get the latest Canonical Ubuntu 22.04 (Jammy) AMI for an instance type.

    Selects the AMI matching the instance type's architecture (x86_64 vs arm64).

    Args:
        ec2: Boto3 EC2 client.
        instance_type: EC2 instance type (used to detect architecture).

    Returns:
        AMI ID or None if not found.
    """
    architecture = get_architecture_for_instance_type(instance_type)
    name_pattern = (
        "ubuntu/images/hvm-ssd-gp3/ubuntu-jammy-22.04-arm64-server-*"
        if architecture == "arm64"
        else "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
    )
    try:
        response = ec2.describe_images(
            Owners=["099720109477"],  # Canonical
            Filters=[
                {"Name": "name", "Values": [name_pattern]},
                {"Name": "state", "Values": ["available"]},
                {"Name": "architecture", "Values": [architecture]},
            ],
        )
    except ClientError as e:
        print(f"Warning: Could not get Ubuntu AMI: {e}", file=sys.stderr)
        return None
    images = sorted(response.get("Images", []), key=lambda image: image["CreationDate"], reverse=True)
    return images[0]["ImageId"] if images else None


SSM_ROLE_TRUST_POLICY = """{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }
    ]
}"""

_SSM_CORE_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"


def create_ssm_instance_profile(iam: Any, description: str = "Temporary role for SSM access") -> tuple[str, str]:
    """Create an IAM role and instance profile granting SSM core access.

    Args:
        iam: Boto3 IAM client.
        description: Role description.

    Returns:
        Tuple of (role_name, profile_name).
    """
    suffix = str(uuid.uuid4())[:8]
    role_name = f"isv-ssm-role-{suffix}"
    profile_name = f"isv-ssm-profile-{suffix}"

    iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=SSM_ROLE_TRUST_POLICY,
        Description=description,
        Tags=[{"Key": "Name", "Value": role_name}, {"Key": "CreatedBy", "Value": "isvtest"}],
    )
    iam.attach_role_policy(RoleName=role_name, PolicyArn=_SSM_CORE_POLICY_ARN)
    iam.create_instance_profile(InstanceProfileName=profile_name)
    iam.add_role_to_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)
    time.sleep(10)  # Wait for IAM role propagation.
    return role_name, profile_name


def delete_ssm_instance_profile(iam: Any, role_name: str, profile_name: str) -> None:
    """Best-effort teardown of an SSM IAM role and instance profile."""
    for fn in (
        lambda: iam.remove_role_from_instance_profile(InstanceProfileName=profile_name, RoleName=role_name),
        lambda: iam.delete_instance_profile(InstanceProfileName=profile_name),
        lambda: iam.detach_role_policy(RoleName=role_name, PolicyArn=_SSM_CORE_POLICY_ARN),
        lambda: iam.delete_role(RoleName=role_name),
    ):
        try:
            fn()
        except ClientError:
            pass


def wait_ssm_ready(ssm: Any, instance_id: str, timeout: int = 180) -> bool:
    """Poll until the SSM agent on an instance reports Online, or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
            info = resp.get("InstanceInformationList", [])
            if info and info[0].get("PingStatus") == "Online":
                return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in TRANSIENT_AWS_CODES:
                raise
            print(f"Warning: describe_instance_information transient error ({code}): {e}", file=sys.stderr)
        time.sleep(10)
    return False


def wait_ssm_ready_all(ssm: Any, instance_ids: list[str], timeout: int = 180) -> list[str]:
    """Poll until every instance's SSM agent reports Online, sharing one deadline.

    Unlike calling :func:`wait_ssm_ready` per host (which costs up to ``timeout``
    seconds *each*, serially), this shares a single deadline so total wait is
    bounded by ``timeout`` regardless of host count, and queries all pending
    instances in one ``describe_instance_information`` call per poll.

    Returns the instance IDs still not Online when the deadline passes (empty
    when all came online), preserving input order.
    """
    if not instance_ids:
        return []
    pending = set(instance_ids)
    deadline = time.time() + timeout
    while True:
        try:
            resp = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": list(pending)}])
            pending -= {
                info["InstanceId"]
                for info in resp.get("InstanceInformationList", [])
                if info.get("PingStatus") == "Online"
            }
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in TRANSIENT_AWS_CODES:
                raise
            print(f"Warning: describe_instance_information transient error ({code}): {e}", file=sys.stderr)
        if not pending or time.time() >= deadline:
            break
        time.sleep(10)
    return [instance_id for instance_id in instance_ids if instance_id in pending]


def run_ssm_command(ssm: Any, instance_id: str, command: str) -> tuple[bool, str]:
    """Run a shell command on an instance via SSM.

    Returns ``(success, output)``. On success ``output`` is stdout, which callers
    parse (ping latency, public IP, ...). On failure ``output`` carries the best
    available diagnostic - stderr, else stdout, else a status/timeout message -
    so callers that surface it as ``error`` keep a usable failure signal instead
    of an empty string.
    """
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
        )
        command_id = resp["Command"]["CommandId"]
        for _ in range(30):
            time.sleep(2)
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            if inv["Status"] in ("Success", "Failed", "TimedOut"):
                stdout = inv.get("StandardOutputContent", "")
                if inv["Status"] == "Success":
                    return True, stdout
                stderr = inv.get("StandardErrorContent", "")
                return False, stderr or stdout or inv["Status"]
        return False, "SSM command did not reach a terminal status (timed out polling)"
    except ClientError as e:
        return False, str(e)


def _parse_ping_latency(output: str) -> float | None:
    """Extract average latency from ping output when present."""
    for line in output.splitlines():
        if "avg" in line:
            parts = line.split("=")[-1].split("/")
            if len(parts) >= 2:
                return float(parts[1])
    return None


def get_architecture_for_instance_type(instance_type: str) -> str:
    """Detect CPU architecture from EC2 instance type.

    Args:
        instance_type: EC2 instance type (e.g., "g5.xlarge", "g5g.xlarge").

    Returns:
        "arm64" for Graviton instances, "x86_64" otherwise.
    """
    family = instance_type.split(".")[0] if "." in instance_type else instance_type

    # Known Graviton GPU instance families
    arm64_families = {"g5g"}

    if family in arm64_families:
        return "arm64"

    # General Graviton detection: ends with 'g' after a digit
    # e.g., c7g, m7g, r7g, t4g - but NOT g4dn, g5, p4d (x86 GPU instances)
    if len(family) >= 2 and family[-1] == "g" and family[-2].isdigit():
        if not family.startswith(("g", "p")):
            return "arm64"

    return "x86_64"
