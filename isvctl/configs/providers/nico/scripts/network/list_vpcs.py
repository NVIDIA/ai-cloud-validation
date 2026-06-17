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

"""List NICo VPCs without mutating them."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.inventory import normalize_vpc
from common.nico_client import NicoAuthError, forge_get_all, resolve_auth


def main() -> int:
    """Fetch NICo VPCs and emit tenant-compatible list fields."""
    parser = argparse.ArgumentParser(description="List NICo VPCs")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    parser.add_argument("--vpc-id", default="", help="Optional target VPC UUID")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "tenants": [],
        "vpcs": [],
        "count": 0,
    }
    if args.vpc_id:
        result["target_tenant"] = args.vpc_id
        result["found_target"] = False

    try:
        auth = resolve_auth()
        raw_vpcs = forge_get_all(
            args.org,
            "vpc",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="vpcs",
        )
        vpcs = [normalize_vpc(vpc) for vpc in raw_vpcs]
        tenants = [{"tenant_id": vpc["tenant_id"], "tenant_name": vpc["tenant_name"]} for vpc in vpcs]

        result["vpcs"] = vpcs
        result["tenants"] = tenants
        result["count"] = len(vpcs)
        if args.vpc_id:
            result["found_target"] = any(
                vpc["tenant_id"] == args.vpc_id or vpc["tenant_name"] == args.vpc_id for vpc in vpcs
            )
        result["success"] = True

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
