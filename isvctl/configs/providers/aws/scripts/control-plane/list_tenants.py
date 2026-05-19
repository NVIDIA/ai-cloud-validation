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

"""List Resource Groups (tenants).

Output JSON:
{
    "success": true,
    "groups": [
        {"tenant_name": "...", "tenant_id": "..."}
    ],
    "found_target": true,
    "target_tenant": "isv-tenant-xxx"
}
"""

import argparse
import json
import os
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--group-name", help="Group name to verify exists")
    args = parser.parse_args()

    rg = boto3.client("resource-groups", region_name=args.region)

    result: dict[str, Any] = {"success": False, "platform": "control_plane", "tenants": []}

    try:
        response = rg.list_groups()
        for g in response.get("GroupIdentifiers", []):
            result["tenants"].append({"tenant_name": g["GroupName"], "tenant_id": g["GroupArn"]})

        if args.group_name:
            result["target_tenant"] = args.group_name
            result["found_target"] = any(t["tenant_name"] == args.group_name for t in result["tenants"])

        result["count"] = len(result["tenants"])
        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
