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

"""NICo credential readiness probe."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get, forge_get_all, resolve_auth


def main() -> int:
    """Validate that configured NICo credentials can read site data."""
    parser = argparse.ArgumentParser(description="Check NICo credentials")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "account_id": args.org,
        "authenticated": False,
        "auth_source": "unresolved",
        "identity_id": f"unresolved:{args.org}",
        "tests": {},
    }

    try:
        auth = resolve_auth()
        result["auth_source"] = auth.source
        result["identity_id"] = f"{auth.source}:{args.org}"
        site = forge_get(args.org, f"site/{args.site_id}", auth.token, base_url=args.api_base)
        sites = forge_get_all(
            args.org,
            "site",
            auth.token,
            base_url=args.api_base,
            params={"pageSize": "100"},
            result_key="sites",
        )

        result["authenticated"] = True
        result["tests"]["identity"] = {
            "passed": bool(site.get("id") or site.get("siteId")),
            "message": f"Authenticated to NICo as org {args.org}",
        }
        result["tests"]["access"] = {
            "passed": isinstance(sites, list),
            "message": "Read access verified with site list",
            "site_count": len(sites),
        }
        result["success"] = result["tests"]["identity"]["passed"] and result["tests"]["access"]["passed"]

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
