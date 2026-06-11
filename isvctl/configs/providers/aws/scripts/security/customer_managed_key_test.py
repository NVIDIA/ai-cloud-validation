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

"""Verify customer-managed key / BYOK support (AWS reference).

The check accepts an existing KMS key via ``--key-id`` or creates a tagged
temporary customer-managed KMS key. It verifies KMS metadata, performs an
encrypt/decrypt roundtrip, creates a small encrypted EBS volume with the key,
then cleans up the temporary volume and any key it created.

Usage:
    python customer_managed_key_test.py --region us-west-2
    python customer_managed_key_test.py --region us-west-2 --key-id <kms-key-id-or-arn>

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "customer_managed_key_test",
    "key_id": "1234abcd-...",
    "key_arn": "arn:aws:kms:...",
    "encrypted_resource_id": "vol-...",
    "encrypted_resource_kms_key_id": "arn:aws:kms:...",
    "tests": { ... }
  }
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError, WaiterError
from common.errors import handle_aws_errors

REQUIRED_TESTS = [
    "customer_managed_key_available",
    "key_manager_is_customer",
    "encrypt_decrypt_roundtrip",
    "resource_encrypted_with_customer_key",
    "provider_managed_key_not_used",
]


def _normalize_optional_key_id(key_id: str | None) -> str | None:
    """Return a stripped key id, treating empty template values as absent."""
    if key_id is None:
        return None
    stripped = key_id.strip()
    return stripped or None


def _failed_tests(error: str) -> dict[str, dict[str, Any]]:
    """Build a failure result for every required BYOK probe."""
    return {name: {"passed": False, "error": error} for name in REQUIRED_TESTS}


def _base_result(region: str) -> dict[str, Any]:
    """Build the common result payload."""
    return {
        "success": False,
        "platform": "security",
        "test_name": "customer_managed_key_test",
        "region": region,
        "key_id": "",
        "key_arn": "",
        "key_manager": "",
        "encrypted_resource_id": "",
        "encrypted_resource_kms_key_id": "",
        "tests": _failed_tests("Validation not executed"),
    }


def _create_customer_managed_key(kms: Any, region: str) -> dict[str, Any]:
    """Create a tagged temporary symmetric KMS key and return its metadata."""
    name = f"isv-byok-test-{uuid.uuid4().hex[:8]}"
    response = kms.create_key(
        Description=f"Temporary ISV BYOK validation key in {region}",
        KeyUsage="ENCRYPT_DECRYPT",
        Origin="AWS_KMS",
        Tags=[
            {"TagKey": "CreatedBy", "TagValue": "isvtest"},
            {"TagKey": "Name", "TagValue": name},
        ],
    )
    return response["KeyMetadata"]


def _check_customer_managed_key_available(metadata: dict[str, Any]) -> dict[str, Any]:
    """Verify the KMS key exists, is enabled, and supports encryption."""
    key_state = metadata.get("KeyState")
    if key_state != "Enabled":
        return {"passed": False, "error": f"KMS key is not enabled (KeyState={key_state!r})"}

    key_usage = metadata.get("KeyUsage")
    if key_usage != "ENCRYPT_DECRYPT":
        return {"passed": False, "error": f"KMS key does not support encrypt/decrypt (KeyUsage={key_usage!r})"}

    return {"passed": True, "message": f"KMS key {metadata.get('KeyId')} is enabled for encrypt/decrypt"}


def _check_key_manager_is_customer(metadata: dict[str, Any]) -> dict[str, Any]:
    """Verify KMS reports the key as customer-managed."""
    key_manager = metadata.get("KeyManager")
    if key_manager != "CUSTOMER":
        return {"passed": False, "error": f"KMS key is not customer-managed (KeyManager={key_manager!r})"}
    return {"passed": True, "message": "KMS KeyManager is CUSTOMER"}


def _check_provider_managed_key_not_used(metadata: dict[str, Any]) -> dict[str, Any]:
    """Verify the selected key is not an AWS-managed provider default."""
    key_manager = metadata.get("KeyManager")
    if key_manager == "AWS":
        return {"passed": False, "error": "AWS-managed KMS key selected instead of a customer-managed key"}
    if key_manager != "CUSTOMER":
        return {"passed": False, "error": f"Unexpected KMS KeyManager={key_manager!r}"}
    return {"passed": True, "message": "Provider-managed default key was not used"}


def _check_encrypt_decrypt_roundtrip(kms: Any, key_id: str) -> dict[str, Any]:
    """Encrypt and decrypt a small payload with the selected KMS key."""
    plaintext = b"isv-customer-managed-key-validation"
    try:
        encrypted = kms.encrypt(KeyId=key_id, Plaintext=plaintext)
        decrypted = kms.decrypt(KeyId=key_id, CiphertextBlob=encrypted["CiphertextBlob"])
    except ClientError as e:
        return {"passed": False, "error": str(e)}

    if decrypted.get("Plaintext") != plaintext:
        return {"passed": False, "error": "KMS decrypt plaintext did not match original payload"}

    return {"passed": True, "message": "KMS encrypt/decrypt roundtrip succeeded"}


def _select_availability_zone(ec2: Any) -> str:
    """Return the first available AZ in the region."""
    response = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
    for zone in response.get("AvailabilityZones", []):
        opt_in_status = zone.get("OptInStatus", "opt-in-not-required")
        if opt_in_status in {"opt-in-not-required", "opted-in"}:
            return zone["ZoneName"]
    msg = "No available availability zone found for encrypted volume test"
    raise RuntimeError(msg)


def _kms_key_matches(actual_key_id: str | None, metadata: dict[str, Any]) -> bool:
    """Return True when an EC2 KmsKeyId refers to the selected KMS key."""
    if not actual_key_id:
        return False

    expected_values = {value for value in (metadata.get("Arn"), metadata.get("KeyId")) if value}
    if actual_key_id in expected_values:
        return True

    key_id = metadata.get("KeyId")
    return bool(key_id and actual_key_id.endswith(f":key/{key_id}"))


def _check_resource_encrypted_with_customer_key(
    ec2: Any,
    key_metadata: dict[str, Any],
    availability_zone: str,
) -> dict[str, Any]:
    """Create a small encrypted EBS volume and verify its KMS key id."""
    volume_id = ""
    try:
        response = ec2.create_volume(
            AvailabilityZone=availability_zone,
            Size=1,
            VolumeType="gp3",
            Encrypted=True,
            KmsKeyId=key_metadata.get("Arn") or key_metadata["KeyId"],
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": "CreatedBy", "Value": "isvtest"},
                        {"Key": "Name", "Value": "isv-byok-test-volume"},
                    ],
                }
            ],
        )
        volume_id = response["VolumeId"]

        waiter = ec2.get_waiter("volume_available")
        waiter.wait(VolumeIds=[volume_id], WaiterConfig={"Delay": 2, "MaxAttempts": 30})

        volume = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
        actual_key_id = volume.get("KmsKeyId") or response.get("KmsKeyId")
        if volume.get("Encrypted") is not True:
            return {"passed": False, "volume_id": volume_id, "error": f"EBS volume {volume_id} is not encrypted"}
        if not _kms_key_matches(actual_key_id, key_metadata):
            return {
                "passed": False,
                "volume_id": volume_id,
                "kms_key_id": actual_key_id,
                "error": f"EBS volume {volume_id} uses unexpected KMS key {actual_key_id!r}",
            }

        return {
            "passed": True,
            "volume_id": volume_id,
            "kms_key_id": actual_key_id,
            "message": f"EBS volume {volume_id} encrypted with customer-managed key",
        }
    except (ClientError, WaiterError) as e:
        return {"passed": False, "volume_id": volume_id, "error": str(e)}
    except Exception as e:
        return {"passed": False, "volume_id": volume_id, "error": str(e)}


def _delete_test_volume(ec2: Any, volume_id: str) -> list[str]:
    """Delete a temporary EBS volume and return cleanup errors."""
    try:
        ec2.delete_volume(VolumeId=volume_id)
    except ClientError as e:
        return [f"delete volume {volume_id}: {e}"]
    return []


def _schedule_key_deletion(kms: Any, key_id: str) -> list[str]:
    """Schedule a temporary KMS key for deletion and return cleanup errors."""
    try:
        kms.schedule_key_deletion(KeyId=key_id, PendingWindowInDays=7)
    except ClientError as e:
        return [f"schedule key deletion {key_id}: {e}"]
    return []


def _run_customer_managed_key_test(
    kms: Any,
    ec2: Any,
    region: str,
    key_id: str | None = None,
) -> dict[str, Any]:
    """Run the BYOK checks with injected AWS clients."""
    result = _base_result(region)
    owned_key = False
    cleanup_key_id = ""
    volume_id = ""

    try:
        if key_id:
            key_metadata = kms.describe_key(KeyId=key_id)["KeyMetadata"]
        else:
            key_metadata = _create_customer_managed_key(kms, region)
            owned_key = True

        cleanup_key_id = key_metadata.get("KeyId", "")
        result["key_id"] = cleanup_key_id
        result["key_arn"] = key_metadata.get("Arn", "")
        result["key_manager"] = key_metadata.get("KeyManager", "")

        result["tests"] = {
            "customer_managed_key_available": _check_customer_managed_key_available(key_metadata),
            "key_manager_is_customer": _check_key_manager_is_customer(key_metadata),
            "encrypt_decrypt_roundtrip": {
                "passed": False,
                "error": "Skipped because key is not an enabled customer-managed key",
            },
            "resource_encrypted_with_customer_key": {
                "passed": False,
                "error": "Skipped because key is not an enabled customer-managed key",
            },
            "provider_managed_key_not_used": _check_provider_managed_key_not_used(key_metadata),
        }

        can_use_key = (
            result["tests"]["customer_managed_key_available"]["passed"]
            and result["tests"]["key_manager_is_customer"]["passed"]
            and result["tests"]["provider_managed_key_not_used"]["passed"]
        )
        if can_use_key:
            result["tests"]["encrypt_decrypt_roundtrip"] = _check_encrypt_decrypt_roundtrip(kms, cleanup_key_id)
            try:
                availability_zone = _select_availability_zone(ec2)
                volume_result = _check_resource_encrypted_with_customer_key(ec2, key_metadata, availability_zone)
            except Exception as e:
                volume_result = {"passed": False, "volume_id": volume_id, "error": str(e)}
            result["tests"]["resource_encrypted_with_customer_key"] = volume_result
            volume_id = volume_result.get("volume_id", "")
            result["encrypted_resource_id"] = volume_id
            result["encrypted_resource_kms_key_id"] = volume_result.get("kms_key_id", "")

        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        result["tests"] = _failed_tests(str(e))
    finally:
        cleanup_errors: list[str] = []
        if volume_id:
            cleanup_errors.extend(_delete_test_volume(ec2, volume_id))
        if owned_key and cleanup_key_id:
            cleanup_errors.extend(_schedule_key_deletion(kms, cleanup_key_id))

        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            cleanup_error = f"Cleanup failed: {'; '.join(cleanup_errors)}"
            result["error"] = f"{result['error']}; {cleanup_error}" if result.get("error") else cleanup_error
            result["success"] = False

    return result


@handle_aws_errors
def main() -> int:
    """Run customer-managed key checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="Customer-managed key / BYOK test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--key-id", default=os.environ.get("AWS_KMS_KEY_ID", ""), help="Existing KMS key id or ARN")
    args = parser.parse_args()

    kms = boto3.client("kms", region_name=args.region)
    ec2 = boto3.client("ec2", region_name=args.region)
    result = _run_customer_managed_key_test(kms, ec2, args.region, _normalize_optional_key_id(args.key_id))

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
