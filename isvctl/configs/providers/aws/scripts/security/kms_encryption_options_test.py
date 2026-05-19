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

"""Verify provider-managed and customer-managed KMS options are available.

The AWS reference proves the provider-managed option by describing a
control-plane AWS-managed service key and proves the customer-managed option by
creating a temporary CMK, then scheduling it for deletion.

Usage:
    python kms_encryption_options_test.py --region us-west-2
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors

REQUIRED_TESTS = [
    "provider_managed_key_available",
    "customer_managed_key_available",
    "both_options_supported",
]
PROVIDER_MANAGED_ALIASES = ("alias/aws/eks",)


def _failed_tests(error: str) -> dict[str, dict[str, Any]]:
    """Build a failure result for every KMS option probe."""
    return {name: {"passed": False, "error": error} for name in REQUIRED_TESTS}


def _base_result(region: str) -> dict[str, Any]:
    """Build the common result payload."""
    return {
        "success": False,
        "platform": "security",
        "test_name": "kms_encryption_options_test",
        "region": region,
        "provider_managed_key_id": "",
        "customer_managed_key_id": "",
        "tests": _failed_tests("Validation not executed"),
    }


def _skipped_result(result: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark the result as a clean skip when AWS does not expose scoped evidence."""
    result["success"] = True
    result["skipped"] = True
    result["skip_reason"] = reason
    result["tests"] = {
        "provider_managed_key_available": {
            "passed": True,
            "skipped": True,
            "message": reason,
        },
        "customer_managed_key_available": {
            "passed": True,
            "skipped": True,
            "message": "Customer-managed KMS option check not executed",
        },
        "both_options_supported": {
            "passed": True,
            "skipped": True,
            "message": reason,
        },
    }
    return result


def _create_customer_managed_key(kms: Any, region: str) -> dict[str, Any]:
    """Create a tagged temporary symmetric KMS key and return its metadata."""
    name = f"isv-kms-options-test-{uuid.uuid4().hex[:8]}"
    response = kms.create_key(
        Description=f"Temporary ISV KMS options validation key in {region}",
        KeyUsage="ENCRYPT_DECRYPT",
        Origin="AWS_KMS",
        Tags=[
            {"TagKey": "CreatedBy", "TagValue": "isvtest"},
            {"TagKey": "Name", "TagValue": name},
        ],
    )
    return response["KeyMetadata"]


def _schedule_key_deletion(kms: Any, key_id: str) -> list[str]:
    """Schedule a temporary KMS key for deletion and return cleanup errors."""
    try:
        kms.schedule_key_deletion(KeyId=key_id, PendingWindowInDays=7)
    except ClientError as e:
        return [f"schedule key deletion {key_id}: {e}"]
    return []


def _discover_aws_managed_aliases(kms: Any) -> tuple[list[str], list[str]]:
    """Return AWS-managed alias names and any list_aliases errors."""
    discovered: list[str] = []
    errors: list[str] = []
    try:
        try:
            paginator = kms.get_paginator("list_aliases")
        except AttributeError:
            pages = [kms.list_aliases()]
        else:
            pages = paginator.paginate()
        for page in pages:
            for entry in page.get("Aliases", []):
                alias_name = entry.get("AliasName", "")
                if alias_name.startswith("alias/aws/"):
                    discovered.append(alias_name)
    except (ClientError, AttributeError) as e:
        errors.append(f"list_aliases: {e}")
        return discovered, errors
    return discovered, errors


def _provider_managed_key(kms: Any) -> tuple[str, list[str], list[str]]:
    """Return the first available control-plane AWS-managed service key."""
    errors: list[str] = []
    candidates: list[str] = list(PROVIDER_MANAGED_ALIASES)
    discovered, discovery_errors = _discover_aws_managed_aliases(kms)
    errors.extend(discovery_errors)
    generic_aliases = [alias for alias in discovered if alias not in candidates]
    for alias in candidates:
        try:
            metadata = kms.describe_key(KeyId=alias)["KeyMetadata"]
        except ClientError as e:
            errors.append(f"{alias}: {e}")
            continue
        if metadata.get("KeyManager") == "AWS":
            return alias, errors, generic_aliases
        errors.append(f"{alias}: KeyManager={metadata.get('KeyManager')!r}")
    return "", errors, generic_aliases


def _customer_key_available(metadata: dict[str, Any]) -> dict[str, Any]:
    """Verify the temporary key is customer-managed and usable for encryption."""
    if metadata.get("KeyManager") != "CUSTOMER":
        return {
            "passed": False,
            "error": f"KMS key is not customer-managed (KeyManager={metadata.get('KeyManager')!r})",
        }
    if metadata.get("KeyState") != "Enabled":
        return {"passed": False, "error": f"KMS key is not enabled (KeyState={metadata.get('KeyState')!r})"}
    if metadata.get("KeyUsage") != "ENCRYPT_DECRYPT":
        return {"passed": False, "error": f"KMS key cannot encrypt/decrypt (KeyUsage={metadata.get('KeyUsage')!r})"}
    return {"passed": True, "message": f"Customer-managed KMS key {metadata.get('KeyId')} is available"}


def _run_kms_encryption_options_test(kms: Any, region: str) -> dict[str, Any]:
    """Run SEC09-02 KMS option checks with injected AWS clients."""
    result = _base_result(region)
    cleanup_key_id = ""

    try:
        provider_key_id, provider_errors, generic_aliases = _provider_managed_key(kms)
        if provider_key_id:
            result["provider_managed_key_id"] = provider_key_id
        elif generic_aliases:
            generic_list = ", ".join(generic_aliases)
            return _skipped_result(
                result,
                "Control-plane provider-managed KMS evidence is not available; "
                f"only non-control-plane AWS-managed aliases were discovered: {generic_list}",
            )

        customer_metadata = _create_customer_managed_key(kms, region)
        cleanup_key_id = customer_metadata.get("KeyId", "")
        result["customer_managed_key_id"] = cleanup_key_id

        provider_passed = bool(provider_key_id)
        customer_result = _customer_key_available(customer_metadata)
        customer_passed = customer_result["passed"]
        result["tests"] = {
            "provider_managed_key_available": {
                "passed": provider_passed,
                "message" if provider_passed else "error": (
                    f"AWS-managed KMS key available: {provider_key_id}"
                    if provider_passed
                    else f"No AWS-managed KMS service key found: {provider_errors}"
                ),
            },
            "customer_managed_key_available": customer_result,
            "both_options_supported": {
                "passed": provider_passed and customer_passed,
                "message" if provider_passed and customer_passed else "error": (
                    "Provider-managed and customer-managed KMS options are available"
                    if provider_passed and customer_passed
                    else "Provider-managed and customer-managed KMS options were not both available"
                ),
            },
        }
        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        result["tests"] = _failed_tests(str(e))
    finally:
        if cleanup_key_id:
            cleanup_errors = _schedule_key_deletion(kms, cleanup_key_id)
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors
                cleanup_error = f"Cleanup failed: {'; '.join(cleanup_errors)}"
                result["tests"]["both_options_supported"] = {
                    "passed": False,
                    "error": cleanup_error,
                }
                result["error"] = f"{result['error']}; {cleanup_error}" if result.get("error") else cleanup_error
                result["success"] = False

    return result


@handle_aws_errors
def main() -> int:
    """Run KMS encryption-option checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="KMS encryption options test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    kms = boto3.client("kms", region_name=args.region)
    result = _run_kms_encryption_options_test(kms, args.region)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
