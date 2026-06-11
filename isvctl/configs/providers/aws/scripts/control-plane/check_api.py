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

"""Check AWS API connectivity and health.

Platform-specific script that uses boto3 to test API endpoints.
Outputs JSON for validation assertions.

Usage:
    python check_api.py --region us-west-2 --services ec2,s3,iam,sts

Output JSON:
{
    "success": true,
    "region": "us-west-2",
    "account_id": "123456789",
    "tests": {
        "sts_identity": {"passed": true, "latency_ms": 123},
        "ec2_api": {"passed": true, "latency_ms": 89},
        "s3_api": {"passed": true, "latency_ms": 156}
    }
}
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


def test_service(session: Any, service: str, region: str) -> dict[str, Any]:
    """Test connectivity to AWS service."""
    result: dict[str, Any] = {"passed": False}
    start = time.time()

    try:
        client = session.client(service, region_name=region)

        # Service-specific read-only operations
        if service == "ec2":
            client.describe_regions(RegionNames=[region])
        elif service == "s3":
            client.list_buckets()
        elif service == "iam":
            client.list_users(MaxItems=1)
        elif service == "sts":
            client.get_caller_identity()
        elif service == "eks":
            client.list_clusters(maxResults=1)
        elif service == "lambda":
            client.list_functions(MaxItems=1)
        elif service == "rds":
            client.describe_db_instances(MaxRecords=20)
        elif service == "dynamodb":
            client.list_tables(Limit=1)

        latency_ms = (time.time() - start) * 1000
        result["passed"] = True
        result["latency_ms"] = round(latency_ms, 2)

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        # Access denied = API is reachable
        if error_code in ["AccessDenied", "AccessDeniedException", "UnauthorizedAccess"]:
            latency_ms = (time.time() - start) * 1000
            result["passed"] = True
            result["latency_ms"] = round(latency_ms, 2)
            result["note"] = "API reachable (access denied)"
        else:
            result["error"] = f"{error_code}: {e.response['Error']['Message']}"
    except NoCredentialsError:
        result["error"] = "No credentials"
    except Exception as e:
        result["error"] = str(e)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AWS API health")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--services", default="ec2,s3,iam,sts", help="Comma-separated services")
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",")]

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "region": args.region,
        "tests": {},
    }

    session = boto3.Session()

    # Test STS first
    sts_result = test_service(session, "sts", args.region)
    result["tests"]["sts_identity"] = sts_result

    if sts_result["passed"]:
        try:
            sts = session.client("sts", region_name=args.region)
            identity = sts.get_caller_identity()
            result["account_id"] = identity["Account"]
            result["arn"] = identity["Arn"]
        except Exception:
            pass

    # Test each service
    for service in services:
        if service != "sts":
            result["tests"][f"{service}_api"] = test_service(session, service, args.region)

    # Count passed
    passed = sum(1 for t in result["tests"].values() if t.get("passed", False))
    total = len(result["tests"])
    result["summary"] = f"{passed}/{total} services reachable"
    result["success"] = result["tests"].get("sts_identity", {}).get("passed", False)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
