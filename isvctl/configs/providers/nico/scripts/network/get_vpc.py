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

"""Describe a NICo VPC without mutating it."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.inventory import first_non_empty_id, normalize_vpc
from common.nico_client import NicoAuthError, forge_get, forge_get_all, resolve_auth


def _first_vpc_id(org: str, site_id: str, token: str, *, base_url: str) -> str:
    """Return the first VPC id for a site, or an empty string."""
    raw_vpcs = forge_get_all(
        org,
        "vpc",
        token,
        base_url=base_url,
        params={"siteId": site_id},
        result_key="vpcs",
    )
    vpcs = [normalize_vpc(vpc) for vpc in raw_vpcs]
    return first_non_empty_id(vpcs, "tenant_id")


def main() -> int:
    """Fetch one NICo VPC and emit tenant-compatible detail fields."""
    parser = argparse.ArgumentParser(description="Get NICo VPC")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    parser.add_argument("--vpc-id", default="", help="VPC UUID; defaults to the first site VPC")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
    }

    try:
        auth = resolve_auth()
        vpc_id = args.vpc_id or _first_vpc_id(args.org, args.site_id, auth.token, base_url=args.api_base)
        if not vpc_id:
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No VPCs found at site; VPC detail validation has no resource to inspect"
            print(json.dumps(result, indent=2))
            return 0

        raw_vpc = forge_get(args.org, f"vpc/{vpc_id}", auth.token, base_url=args.api_base)
        result.update(normalize_vpc(raw_vpc))
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
