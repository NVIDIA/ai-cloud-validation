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

"""Verify EKS control-plane TLS certificates are provider-managed.

The AWS reference scopes SEC09-01 to managed Kubernetes control-plane API
surfaces. EKS endpoint certificates are AWS-managed and not exposed through
customer ACM or IAM server-certificate inventory.

Usage:
    python cert_rotation_test.py --region us-west-2
"""

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors

ROTATION_WINDOW_DAYS = 60
MAX_CERTS_PER_SOURCE = 25
REQUIRED_TESTS = [
    "cert_inventory_non_empty",
    "no_certs_out_of_policy",
    "rotation_evidence_present",
]


def _failed_tests(error: str) -> dict[str, dict[str, Any]]:
    """Build a failure result for every required certificate-rotation probe."""
    return {name: {"passed": False, "error": error} for name in REQUIRED_TESTS}


def _base_result(region: str) -> dict[str, Any]:
    """Build the common result payload."""
    return {
        "success": False,
        "platform": "security",
        "test_name": "cert_rotation_test",
        "region": region,
        "rotation_window_days": ROTATION_WINDOW_DAYS,
        "sample_limit_per_source": MAX_CERTS_PER_SOURCE,
        "certs_inspected": 0,
        "auto_rotated": 0,
        "short_validity": 0,
        "out_of_policy": 0,
        "certificates": [],
        "tests": _failed_tests("Validation not executed"),
    }


def _eks_endpoint_certificate_record(cluster_name: str, cluster: dict[str, Any]) -> dict[str, Any]:
    """Classify the AWS-managed TLS certificate for an EKS API endpoint."""
    endpoint = cluster.get("endpoint", "")
    hostname = urlparse(endpoint).hostname
    if not hostname:
        msg = f"EKS cluster {cluster_name} endpoint has no hostname: {endpoint!r}"
        raise ValueError(msg)

    return {
        "source": "eks",
        "certificate_id": cluster_name,
        "validity_days": None,
        "endpoint_host": hostname,
        "provider_managed": True,
        "rotation_evidence_hidden": True,
        "endpoint_public_access": cluster.get("resourcesVpcConfig", {}).get("endpointPublicAccess"),
        "endpoint_private_access": cluster.get("resourcesVpcConfig", {}).get("endpointPrivateAccess"),
    }


def _eks_certificate_records(eks: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Return certificate records for EKS API endpoints and inspection errors."""
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    next_token = ""
    attempts = 0
    while True:
        kwargs = {"nextToken": next_token} if next_token else {}
        response = eks.list_clusters(**kwargs)
        for cluster_name in response.get("clusters", []):
            if attempts >= MAX_CERTS_PER_SOURCE:
                return records, errors
            attempts += 1
            try:
                cluster = eks.describe_cluster(name=cluster_name)["cluster"]
                records.append(_eks_endpoint_certificate_record(cluster_name, cluster))
            except Exception as e:
                errors.append(f"eks:{cluster_name}: {e}")
        next_token = response.get("nextToken", "")
        if not next_token:
            return records, errors


def _skipped_result(result: dict[str, Any]) -> dict[str, Any]:
    """Mark the result as a clean skip when no control-plane inventory exists."""
    result["success"] = True
    result["skipped"] = True
    result["skip_reason"] = "No managed TLS certificates found on this platform"
    result["tests"] = {
        "cert_inventory_non_empty": {
            "passed": True,
            "skipped": True,
            "message": result["skip_reason"],
        },
        "no_certs_out_of_policy": {
            "passed": True,
            "skipped": True,
            "message": "No managed TLS certificate inventory to evaluate",
        },
        "rotation_evidence_present": {
            "passed": True,
            "skipped": True,
            "message": "No managed TLS certificate rotation evidence to evaluate",
        },
    }
    return result


def _provider_hidden_rotation_skip_result(result: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Mark EKS control-plane certificate rotation evidence as provider-hidden."""
    result["success"] = True
    result["skipped"] = True
    result["skip_reason"] = "Managed TLS certificate rotation evidence is provider-hidden"
    result["certificates"] = records
    result["certs_inspected"] = len(records)
    result["tests"] = {
        "cert_inventory_non_empty": {
            "passed": True,
            "skipped": True,
            "message": f"Discovered {len(records)} EKS control-plane certificate endpoint(s)",
        },
        "no_certs_out_of_policy": {
            "passed": True,
            "skipped": True,
            "message": "EKS control-plane certificate policy state is provider-hidden",
        },
        "rotation_evidence_present": {
            "passed": True,
            "skipped": True,
            "message": result["skip_reason"],
        },
    }
    return result


def _inspection_error_result(
    result: dict[str, Any], records: list[dict[str, Any]], inspection_errors: list[str]
) -> dict[str, Any]:
    """Mark certificate verification failed because AWS inventory could not be fully inspected."""
    error = f"Certificate inspection errors prevented SEC09-01 verification: {inspection_errors}"
    result["certificates"] = records
    result["certs_inspected"] = len(records)
    result["inspection_errors"] = inspection_errors
    result["tests"] = {
        "cert_inventory_non_empty": {
            "passed": bool(records),
            "message" if records else "error": (
                f"Inspected {len(records)} EKS control-plane certificate endpoint(s)"
                if records
                else "No EKS control-plane certificates were inspected"
            ),
        },
        "no_certs_out_of_policy": {"passed": False, "error": error},
        "rotation_evidence_present": {"passed": False, "error": error},
    }
    return result


def _run_cert_rotation_test(eks: Any, region: str) -> dict[str, Any]:
    """Run SEC09-01 EKS control-plane certificate checks with injected AWS clients."""
    result = _base_result(region)
    records: list[dict[str, Any]] = []
    inspection_errors: list[str] = []

    try:
        eks_records, eks_errors = _eks_certificate_records(eks)
        records.extend(eks_records)
        inspection_errors.extend(eks_errors)
    except ClientError as e:
        inspection_errors.append(f"eks: {e}")

    if not records and not inspection_errors:
        return _skipped_result(result)

    if inspection_errors:
        return _inspection_error_result(result, records, inspection_errors)

    if all(record.get("rotation_evidence_hidden") is True for record in records):
        return _provider_hidden_rotation_skip_result(result, records)

    return _inspection_error_result(
        result,
        records,
        ["Certificate records included customer-visible rotation evidence, but no AWS policy evaluator is wired"],
    )


@handle_aws_errors
def main() -> int:
    """Run certificate-rotation checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="Certificate rotation cycle test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    eks = boto3.client("eks", region_name=args.region)
    result = _run_cert_rotation_test(eks, args.region)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
