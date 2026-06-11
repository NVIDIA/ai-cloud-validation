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

"""Delete access key and user.

Output JSON:
{
    "success": true,
    "deleted_key": "AKIA...",
    "deleted_user": "isv-test-xxx"
}
"""

import argparse
import json
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--region", help="AWS region (IAM is global; used for endpoint routing)")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        print(json.dumps(result, indent=2))
        return 0

    iam = boto3.client("iam", region_name=args.region) if args.region else boto3.client("iam")

    try:
        # Delete access key
        iam.delete_access_key(UserName=args.username, AccessKeyId=args.access_key_id)
        result["deleted_key"] = args.access_key_id

        # Delete user
        iam.delete_user(UserName=args.username)
        result["deleted_user"] = args.username
        result["success"] = True

    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            result["success"] = True
            result["already_deleted"] = True
        else:
            result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
