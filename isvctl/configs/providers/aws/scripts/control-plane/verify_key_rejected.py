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

"""Verify disabled access key is rejected.

Output JSON:
{
    "success": true,
    "rejected": true,
    "error_code": "InvalidClientTokenId"
}
"""

import argparse
import json
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--secret-access-key", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--wait", type=int, default=5, help="Initial wait for propagation")
    parser.add_argument("--retries", type=int, default=5, help="Number of retry attempts")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane", "rejected": False}

    # Wait for initial key status to propagate
    if args.wait > 0:
        time.sleep(args.wait)

    # Retry with exponential backoff - IAM propagation can take 10-20 seconds
    for attempt in range(args.retries):
        try:
            session = boto3.Session(
                aws_access_key_id=args.access_key_id,
                aws_secret_access_key=args.secret_access_key,
                region_name=args.region,
            )
            sts = session.client("sts")
            sts.get_caller_identity()

            # Key still active - retry if attempts remaining
            if attempt < args.retries - 1:
                time.sleep(2 ** (attempt + 1))  # 2, 4, 8, 16 seconds
                continue

            # Final attempt - key still active
            result["rejected"] = False
            result["error"] = "Key was not rejected after retries - still active"

        except ClientError as e:
            # Key was rejected - this is the expected behavior
            result["rejected"] = True
            result["error_code"] = e.response["Error"]["Code"]
            result["success"] = True
            break

    print(json.dumps(result, indent=2))
    # Always exit 0 - let validation check the 'rejected' field
    return 0


if __name__ == "__main__":
    sys.exit(main())
