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

"""Describe a NICo instance without mutating it."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.inventory import first_non_empty_id, normalize_instance
from common.nico_client import NicoAuthError, forge_get, forge_get_all, resolve_auth


def _first_instance_id(org: str, site_id: str, token: str, *, base_url: str) -> str:
    """Return the first instance id for a site, or an empty string."""
    raw_instances = forge_get_all(
        org,
        "instance",
        token,
        base_url=base_url,
        params={"siteId": site_id},
        result_key="instances",
    )
    instances = [normalize_instance(instance) for instance in raw_instances]
    return first_non_empty_id(instances, "instance_id")


def main() -> int:
    """Fetch one NICo instance and emit the InstanceStateCheck contract."""
    parser = argparse.ArgumentParser(description="Describe NICo instance")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    parser.add_argument("--instance-id", default="", help="Instance UUID; defaults to the first site instance")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
    }

    try:
        auth = resolve_auth()
        instance_id = args.instance_id or _first_instance_id(args.org, args.site_id, auth.token, base_url=args.api_base)
        if not instance_id:
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No instances found at site; instance detail validation has no resource to inspect"
            print(json.dumps(result, indent=2))
            return 0

        raw_instance = forge_get(args.org, f"instance/{instance_id}", auth.token, base_url=args.api_base)
        result.update(normalize_instance(raw_instance))
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
