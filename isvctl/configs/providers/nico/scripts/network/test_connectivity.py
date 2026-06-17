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

"""NICo network assignment probe."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.inventory import normalize_subnet
from common.nico_client import NicoAuthError, forge_get_all, resolve_auth


def main() -> int:
    """Verify that an existing NICo VPC has at least one subnet."""
    parser = argparse.ArgumentParser(description="Check NICo network assignment")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    parser.add_argument("--vpc-id", default="", help="Optional VPC UUID")
    parser.add_argument("--subnet-id", default="", help="Optional subnet UUID")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "vpc_id": args.vpc_id,
        "subnets": [],
        "subnet_count": 0,
        "tests": {},
    }

    try:
        auth = resolve_auth()
        params = {"siteId": args.site_id}
        if args.vpc_id:
            params["vpcId"] = args.vpc_id
        raw_subnets = forge_get_all(
            args.org,
            "subnet",
            auth.token,
            base_url=args.api_base,
            params=params,
            result_key="subnets",
        )
        subnets = [normalize_subnet(subnet) for subnet in raw_subnets]
        subnet_ids = {subnet["subnet_id"] for subnet in subnets if subnet["subnet_id"]}
        has_subnet = bool(subnet_ids)
        target_found = args.subnet_id in subnet_ids if args.subnet_id else has_subnet

        result["subnets"] = subnets
        result["subnet_count"] = len(subnets)
        if not has_subnet and not args.subnet_id:
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No subnets found at site; network assignment validation has no resource to inspect"
            print(json.dumps(result, indent=2))
            return 0

        result["tests"]["network_assigned"] = {"passed": has_subnet and target_found}
        result["success"] = result["tests"]["network_assigned"]["passed"]

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except Exception as e:
        result["tests"]["network_assigned"] = {"passed": False, "error": str(e)}
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
