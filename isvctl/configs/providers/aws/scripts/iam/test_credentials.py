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

"""Test IAM credentials by making API calls.

Platform-specific script that uses boto3 to validate credentials.
Outputs JSON for validation assertions.

Usage:
    python test_credentials.py --access-key-id AKIA... --secret-access-key xxx

Output JSON:
{
    "success": true,
    "account_id": "123456789",
    "arn": "arn:aws:iam::...",
    "tests": {
        "sts_identity": {"passed": true},
        "iam_access": {"passed": true}
    }
}
"""

import argparse
import json
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import classify_aws_error, handle_aws_errors

# IAM has eventual consistency - new keys may take a few seconds to propagate
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 2


def test_sts_identity(session: boto3.Session, result: dict) -> bool:
    """Test STS GetCallerIdentity with retries for eventual consistency.

    Args:
        session: Boto3 session with credentials
        result: Result dict to update with identity info

    Returns:
        True if successful, False otherwise (updates result in place)
    """
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            sts = session.client("sts")
            identity = sts.get_caller_identity()
            result["account_id"] = identity["Account"]
            result["arn"] = identity["Arn"]
            result["user_id"] = identity["UserId"]
            result["tests"]["sts_identity"] = {"passed": True}
            if attempt > 0:
                result["tests"]["sts_identity"]["retries"] = attempt
            return True
        except ClientError as e:
            last_error = e
            # Don't retry on permanent credential errors
            # But DO retry on InvalidClientTokenId - that's IAM eventual consistency
            error_type, _ = classify_aws_error(e)
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_type in ("credentials_expired", "credentials_missing") and error_code != "InvalidClientTokenId":
                break
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    if last_error:
        error_type, error_msg = classify_aws_error(last_error)
        result["error_type"] = error_type
        result["error"] = error_msg
        result["tests"]["sts_identity"] = {"passed": False, "error": error_msg}
    return False


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test IAM credentials")
    parser.add_argument("--access-key-id", required=True, help="AWS access key ID")
    parser.add_argument("--secret-access-key", required=True, help="AWS secret access key")
    parser.add_argument("--region", default="us-west-2", help="AWS region")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "iam",
        "tests": {},
    }

    session = boto3.Session(
        aws_access_key_id=args.access_key_id,
        aws_secret_access_key=args.secret_access_key,
        region_name=args.region,
    )

    # Test STS GetCallerIdentity (with retries for eventual consistency)
    if not test_sts_identity(session, result):
        print(json.dumps(result, indent=2))
        return 1

    # Test IAM access
    try:
        iam = session.client("iam")
        iam.get_user()
        result["tests"]["iam_access"] = {"passed": True}
    except ClientError as e:
        if e.response["Error"]["Code"] == "AccessDenied":
            # Access denied means credentials work but limited permissions
            result["tests"]["iam_access"] = {"passed": True, "note": "Access denied (expected)"}
        else:
            result["tests"]["iam_access"] = {"passed": False, "error": str(e)}

    result["success"] = result["tests"]["sts_identity"]["passed"]
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
