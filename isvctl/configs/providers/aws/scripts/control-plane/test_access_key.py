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

"""Test access key authentication.

Output JSON:
{
    "success": true,
    "authenticated": true,
    "identity_id": "arn:aws:iam::...",
    "account_id": "123456789"
}
"""

import argparse
import json
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--secret-access-key", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--wait", type=int, default=5, help="Seconds to wait for key propagation")
    parser.add_argument("--retries", type=int, default=3, help="Number of retry attempts")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane", "authenticated": False}

    # Wait for initial key propagation
    if args.wait > 0:
        time.sleep(args.wait)

    # Retry with exponential backoff
    last_error = None
    for attempt in range(args.retries):
        try:
            session = boto3.Session(
                aws_access_key_id=args.access_key_id,
                aws_secret_access_key=args.secret_access_key,
                region_name=args.region,
            )
            sts = session.client("sts")
            identity = sts.get_caller_identity()

            result["authenticated"] = True
            result["identity_id"] = identity["Arn"]
            result["account_id"] = identity["Account"]
            result["success"] = True
            break

        except (ClientError, NoCredentialsError) as e:
            last_error = str(e)
            if attempt < args.retries - 1:
                # Wait before retry (exponential backoff: 2, 4, 8 seconds)
                time.sleep(2 ** (attempt + 1))

    if not result["success"]:
        result["error"] = last_error
        result["authenticated"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
