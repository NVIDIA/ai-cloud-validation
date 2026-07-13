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

"""Prove a specified SSH key can access SOL (serial console) on AWS EC2.

AUTH03-01: after AUTH02 launches an instance with a requested key, push that
key's public material via EC2 Instance Connect serial-console authorization.
Tenant-visible network-device SSH is not offered on AWS, so that probe is
marked provider-hidden.

Usage:
    python component_key_access.py --instance-id i-xxx --key-file /tmp/key.pem \\
        --key-name isv-test-key --region us-west-2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors
from common.serial_console import check_serial_access
from paramiko import ECDSAKey, Ed25519Key, RSAKey
from paramiko.ssh_exception import SSHException

SERIAL_ACCESS_DISABLED_SKIP_REASON = "EC2 serial console access is disabled for this account or region"
NETWORK_DEVICE_HIDDEN_MESSAGE = "AWS does not expose tenant-visible network-device SSH for key-based access"
REQUIRED_TESTS = ("sol_access", "network_device_access")


def _make_test_result(passed: bool, **details: Any) -> dict[str, Any]:
    """Build a standard test result dictionary."""
    return {"passed": passed, **details}


def _mark_skipped(result: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark the whole AUTH03 probe as skipped with skipped subtest evidence."""
    result["success"] = True
    result["skipped"] = True
    result["skip_reason"] = reason
    result["tests"] = {
        name: {
            "passed": True,
            "skipped": True,
            "skip_reason": reason,
        }
        for name in REQUIRED_TESTS
    }
    return result


def _load_openssh_public_key(key_file: str) -> str:
    """Derive an OpenSSH public-key line from a private key PEM file."""
    path = Path(key_file)
    if not path.is_file():
        msg = f"Key file not found: {key_file}"
        raise FileNotFoundError(msg)

    loaders = (RSAKey, ECDSAKey, Ed25519Key)
    last_error: Exception | None = None
    for loader in loaders:
        try:
            private_key = loader.from_private_key_file(str(path))
            return f"{private_key.get_name()} {private_key.get_base64()}"
        except (SSHException, OSError, ValueError) as exc:
            last_error = exc

    msg = f"Unable to load private key from {key_file}: {last_error}"
    raise ValueError(msg)


def _probe_sol_access(ec2: Any, eic: Any, instance_id: str, ssh_public_key: str) -> dict[str, Any]:
    """Authorize the specified public key for EC2 serial-console SSH."""
    serial_access = check_serial_access(ec2)
    if serial_access.get("error"):
        return _make_test_result(False, error=serial_access["error"], probes=["serial_console_access"])
    if serial_access.get("enabled") is not True:
        return _make_test_result(
            False,
            skipped=True,
            skip_reason=SERIAL_ACCESS_DISABLED_SKIP_REASON,
            probes=["serial_console_access"],
        )

    try:
        response = eic.send_serial_console_ssh_public_key(
            InstanceId=instance_id,
            SSHPublicKey=ssh_public_key,
            SerialPort=1,
        )
    except ClientError as exc:
        return _make_test_result(False, error=str(exc), probes=["send_serial_console_ssh_public_key"])

    success = bool(response.get("Success")) if isinstance(response, dict) else False
    request_id = ""
    if isinstance(response, dict):
        request_id = str(response.get("RequestId") or response.get("ResponseMetadata", {}).get("RequestId", ""))

    return _make_test_result(
        success,
        message="Authorized specified key for EC2 serial console SSH",
        probes=["serial_console_access", "send_serial_console_ssh_public_key"],
        request_id=request_id or None,
    )


def _probe_network_device_access() -> dict[str, Any]:
    """AWS has no tenant-visible network-device key path; mark provider-hidden."""
    return _make_test_result(
        True,
        provider_hidden=True,
        message=NETWORK_DEVICE_HIDDEN_MESSAGE,
        probes=["network_device_ssh"],
    )


@handle_aws_errors
def main() -> int:
    """Run AUTH03 key-based component access probes and emit JSON."""
    parser = argparse.ArgumentParser(description="Prove specified key access to SOL / network devices")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--key-file", required=True, help="Path to the instance private key PEM")
    parser.add_argument("--key-name", required=True, help="Key pair name requested at launch (AUTH02)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "test_name": "component_key_access",
        "instance_id": args.instance_id,
        "key_name": args.key_name,
        "tests": {},
    }

    try:
        ssh_public_key = _load_openssh_public_key(args.key_file)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        result["tests"]["sol_access"] = _make_test_result(False, error=str(exc))
        result["tests"]["network_device_access"] = _probe_network_device_access()
        print(json.dumps(result, indent=2))
        return 1

    ec2 = boto3.client("ec2", region_name=args.region)
    eic = boto3.client("ec2-instance-connect", region_name=args.region)

    sol = _probe_sol_access(ec2, eic, args.instance_id, ssh_public_key)
    if sol.get("skipped") is True:
        print(json.dumps(_mark_skipped(result, sol.get("skip_reason") or SERIAL_ACCESS_DISABLED_SKIP_REASON), indent=2))
        return 0

    result["tests"]["sol_access"] = sol
    result["tests"]["network_device_access"] = _probe_network_device_access()
    result["success"] = sol.get("passed") is True and result["tests"]["network_device_access"].get("passed") is True
    if not result["success"] and sol.get("error"):
        result["error"] = sol["error"]

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
