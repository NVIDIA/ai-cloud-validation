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

"""Create Resource Group (tenant).

Output JSON:
{
    "success": true,
    "tenant_name": "isv-tenant-xxx",
    "tenant_id": "arn:aws:resource-groups:..."
}
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--name-prefix", default="isv-tenant-test")
    args = parser.parse_args()

    rg = boto3.client("resource-groups", region_name=args.region)
    group_name = f"{args.name_prefix}-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {"success": False, "platform": "control_plane", "tenant_name": group_name}

    try:
        response = rg.create_group(
            Name=group_name,
            Description="ISV Lab test tenant group",
            ResourceQuery={
                "Type": "TAG_FILTERS_1_0",
                "Query": json.dumps(
                    {
                        "ResourceTypeFilters": ["AWS::AllSupported"],
                        "TagFilters": [{"Key": "isv-tenant", "Values": [group_name]}],
                    }
                ),
            },
            Tags={"CreatedBy": "isvtest", "purpose": "tenant-lifecycle-test"},
        )
        result["tenant_id"] = response["Group"]["GroupArn"]
        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
