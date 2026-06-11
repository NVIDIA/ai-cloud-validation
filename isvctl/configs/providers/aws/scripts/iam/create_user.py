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

"""Create IAM user for testing.

Usage:
    python create_user.py --username test-user

Output JSON:
{
    "success": true,
    "username": "test-user-abc123",
    "user_arn": "arn:aws:iam::123456789:user/test-user-abc123",
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
}
"""

import argparse
import json
import sys
import uuid

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from common.errors import handle_aws_errors


@handle_aws_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Create IAM user")
    parser.add_argument("--username", default="isv-test-user", help="Username prefix")
    parser.add_argument("--create-access-key", action="store_true", default=True)
    args = parser.parse_args()

    # Generate unique username
    suffix = str(uuid.uuid4())[:8]
    username = f"{args.username}-{suffix}"

    result = {
        "success": False,
        "platform": "iam",
        "username": username,
    }

    iam = boto3.client("iam")

    # Create user
    response = iam.create_user(
        UserName=username,
        Tags=[
            {"Key": "CreatedBy", "Value": "isvtest"},
        ],
    )
    result["user_arn"] = response["User"]["Arn"]
    result["user_id"] = response["User"]["UserId"]

    # Create access key
    if args.create_access_key:
        key_response = iam.create_access_key(UserName=username)
        result["access_key_id"] = key_response["AccessKey"]["AccessKeyId"]
        result["secret_access_key"] = key_response["AccessKey"]["SecretAccessKey"]

    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
