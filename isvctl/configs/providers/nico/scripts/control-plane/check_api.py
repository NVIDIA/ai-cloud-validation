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

"""NICo API health probe."""

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get, forge_get_all, resolve_auth


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    """Run a callable and return its result with elapsed milliseconds."""
    start = time.monotonic()
    payload = call()
    return payload, round((time.monotonic() - start) * 1000, 2)


def main() -> int:
    """Authenticate to NICo and verify site endpoints are reachable."""
    parser = argparse.ArgumentParser(description="Check NICo API health")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "account_id": args.org,
        "tests": {},
    }

    try:
        auth = resolve_auth()
        site, site_latency = _timed(
            lambda: forge_get(
                args.org,
                f"site/{args.site_id}",
                auth.token,
                base_url=args.api_base,
            )
        )
        sites, sites_latency = _timed(
            lambda: forge_get_all(
                args.org,
                "site",
                auth.token,
                base_url=args.api_base,
                params={"pageSize": "100"},
                result_key="sites",
            )
        )

        result["tests"]["site"] = {
            "passed": bool(site.get("id") or site.get("siteId")),
            "latency_ms": site_latency,
        }
        result["tests"]["sites"] = {
            "passed": isinstance(sites, list),
            "latency_ms": sites_latency,
            "count": len(sites),
        }
        result["success"] = all(test["passed"] for test in result["tests"].values())

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
