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

"""List NICo instances without mutating them."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.inventory import normalize_instance
from common.nico_client import NicoAuthError, forge_get_all, resolve_auth


def main() -> int:
    """Fetch NICo instances for a site and emit the InstanceListCheck contract."""
    parser = argparse.ArgumentParser(description="List NICo instances")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    parser.add_argument("--instance-id", default="", help="Optional target instance UUID")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "instances": [],
        "count": 0,
    }
    if args.instance_id:
        result["target_instance"] = args.instance_id
        result["found_target"] = False

    try:
        auth = resolve_auth()
        raw_instances = forge_get_all(
            args.org,
            "instance",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="instances",
        )
        instances = [normalize_instance(instance) for instance in raw_instances]

        result["instances"] = instances
        result["count"] = len(instances)
        if args.instance_id:
            result["found_target"] = any(instance["instance_id"] == args.instance_id for instance in instances)
        elif not instances:
            result["skipped"] = True
            result["skip_reason"] = (
                "No instances found at site; instance inventory validation has no resource to inspect"
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
