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

"""Verify out-of-cluster service accounts can obtain credentials and authenticate.

The property under test is that the service account can authenticate as the
expected identity; the credential may come from any source the platform supports
(long-lived key, short-lived token, impersonation, workload-identity federation),
reported in credential_source. AWS reference implementation: creates a temporary
IAM user with programmatic access (long-lived access key, credential_source
"long_lived_key"), authenticates with STS GetCallerIdentity, then cleans up.

Usage:
    python sa_credential_test.py --region us-west-2

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "sa_credential_test",
    "authenticated": true,
    "credential_type": "access_key",
    "credential_source": "long_lived_key",
    "identity": "arn:aws:iam::123456789012:user/isv-sa-test-xxxx",
    "expires_at": null
  }
"""

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors


def _cleanup_test_user(
    iam: Any,
    username: str,
    access_key_id: str | None,
    user_created: bool,
) -> list[str]:
    """Delete test credentials and user, returning cleanup errors."""
    cleanup_errors: list[str] = []

    if access_key_id:
        try:
            iam.delete_access_key(UserName=username, AccessKeyId=access_key_id)
        except ClientError as e:
            cleanup_errors.append(f"delete access key {access_key_id} for {username}: {e}")

    if user_created:
        try:
            iam.delete_user(UserName=username)
        except ClientError as e:
            cleanup_errors.append(f"delete user {username}: {e}")

    return cleanup_errors


@handle_aws_errors
def main() -> int:
    """Run service account credential authentication test and emit JSON result."""
    parser = argparse.ArgumentParser(description="Service account credential test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    iam = boto3.client("iam", region_name=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "sa_credential_test",
        "authenticated": False,
        "credential_type": "",
        "credential_source": "long_lived_key",
        "identity": "",
        "expires_at": None,
    }

    username = f"isv-sa-test-{uuid.uuid4().hex[:8]}"
    access_key_id = None
    user_created = False

    try:
        iam.create_user(
            UserName=username,
            Tags=[{"Key": "CreatedBy", "Value": "isvtest"}],
        )
        user_created = True

        key_response = iam.create_access_key(UserName=username)
        access_key_id = key_response["AccessKey"]["AccessKeyId"]
        secret_key = key_response["AccessKey"]["SecretAccessKey"]

        result["credential_type"] = "access_key"

        sts = boto3.client(
            "sts",
            region_name=args.region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_key,
        )

        # IAM is eventually consistent - new keys can take 15-30s to
        # propagate to STS.  Retry with exponential backoff capped at 8s
        # (2, 4, 8, 8, 8, 8, 8 = 46s total worst case before final attempt).
        max_attempts = 8
        for attempt in range(max_attempts):
            try:
                identity = sts.get_caller_identity()
                result["authenticated"] = True
                result["identity"] = identity["Arn"]
                result["success"] = True
                break
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "InvalidClientTokenId" and attempt < max_attempts - 1:
                    time.sleep(min(2 ** (attempt + 1), 8))
                    continue
                raise

    except ClientError as e:
        result["error"] = str(e)
    finally:
        cleanup_errors = _cleanup_test_user(iam, username, access_key_id, user_created)
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            cleanup_error = f"Cleanup failed: {'; '.join(cleanup_errors)}"
            result["error"] = f"{result['error']}; {cleanup_error}" if result.get("error") else cleanup_error
            result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
