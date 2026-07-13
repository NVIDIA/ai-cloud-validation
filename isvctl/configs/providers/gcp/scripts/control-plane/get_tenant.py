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

"""Retrieve a tenant grouping resource (Resource Manager TagValue) by name.

The AWS reference retrieves a Resource Group by its forwarded name and emits its
canonical name, permanent id, and description. On GCP,
``TagValuesClient.get_namespaced_tag_value`` resolves the output-only
``namespaced_name`` to the permanent TagValue and returns its metadata.

Usage:
    python3 get_tenant.py --region us-central1 \
        --group-name my-project/isv-tenant-1a2b3c4d/isv-tenant-val-1a2b3c4d

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "tenant_name": "my-project/isv-tenant-1a2b3c4d/isv-tenant-val-1a2b3c4d",
    "tenant_id": "tagValues/123456789012",
    "description": "ISV control-plane tenant-lifecycle test value"
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.errors import handle_gcp_errors, retry_idempotent
from google.api_core import exceptions as gax
from google.cloud import resourcemanager_v3

# Non-empty sentinel the provider config renders (via `default(..., true)`) for
# --group-name when create_tenant produced no tenant_name. Handled explicitly as
# a structured failure. Must match the literal in config/control-plane.yaml.
_MISSING_TENANT_SENTINEL = "__no_tenant__"


@handle_gcp_errors
def main() -> int:
    """Retrieve the target TagValue and print its canonical tenant metadata."""
    parser = argparse.ArgumentParser(description="Get control-plane tenant info (Resource Manager tags)")
    parser.add_argument("--group-name", required=True, help="Target TagValue namespaced name from create_tenant")
    parser.add_argument("--region", default="", help="Accepted for contract parity; tags are global")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenant_name": "",
        "tenant_id": "",
        "description": "",
    }

    if args.group_name == _MISSING_TENANT_SENTINEL:
        # create_tenant produced no tenant name (setup failed/skipped). There is no
        # target to retrieve; report a clear structured failure rather than issuing
        # an invalid-name lookup.
        result["error"] = "no tenant name from setup (create_tenant produced none); nothing to get"
        print(json.dumps(result, indent=2))
        return 1

    tv_client = resourcemanager_v3.TagValuesClient()

    try:
        value = retry_idempotent(
            tv_client.get_namespaced_tag_value,
            name=args.group_name,
            op_desc="get_namespaced_tag_value",
        )
    except gax.NotFound:
        result["error"] = f"tenant '{args.group_name}' not found"
        print(json.dumps(result, indent=2))
        return 1

    result["tenant_name"] = value.namespaced_name
    result["tenant_id"] = value.name
    result["description"] = value.description
    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
