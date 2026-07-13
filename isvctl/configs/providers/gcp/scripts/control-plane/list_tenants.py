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

"""List tenant grouping resources (Resource Manager TagValues) in the target scope.

The AWS reference lists Resource Groups and proves the created target is present.
On GCP, ``TagValuesClient.list_tag_values`` requires the permanent parent TagKey
name, so the forwarded ``--group-name`` (the target TagValue namespaced name) is
resolved first to obtain that parent. Every TagValue under the parent is projected
to the canonical ``tenant_name`` / ``tenant_id`` shape, and ``found_target`` is
derived by exact permanent-name comparison (never hardcoded).

Usage:
    python3 list_tenants.py --region us-central1 \
        --group-name my-project/isv-tenant-1a2b3c4d/isv-tenant-val-1a2b3c4d

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "tenants": [{"tenant_name": "...", "tenant_id": "tagValues/..."}],
    "count": 1,
    "target_tenant": "my-project/isv-tenant-1a2b3c4d/isv-tenant-val-1a2b3c4d",
    "found_target": true
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.errors import classify_gcp_error, handle_gcp_errors, retry_idempotent, retry_idempotent_list
from google.api_core import exceptions as gax
from google.cloud import resourcemanager_v3

# Non-empty sentinel the provider config renders (via `default(..., true)`) for
# --group-name when create_tenant produced no tenant_name. Handled explicitly as
# a structured failure. Must match the literal in config/control-plane.yaml.
_MISSING_TENANT_SENTINEL = "__no_tenant__"


@handle_gcp_errors
def main() -> int:
    """List TagValues under the target tenant's parent and print a structured result."""
    parser = argparse.ArgumentParser(description="List control-plane tenants (Resource Manager tags)")
    parser.add_argument("--region", default="", help="Accepted for contract parity; tags are global")
    parser.add_argument("--group-name", required=True, help="Target TagValue namespaced name from create_tenant")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenants": [],
        "count": 0,
        "target_tenant": args.group_name,
        "found_target": False,
    }

    if args.group_name == _MISSING_TENANT_SENTINEL:
        # create_tenant produced no tenant name (setup failed/skipped). There is no
        # target to list; report a clear structured failure rather than issuing an
        # invalid-name lookup.
        result["error"] = "no tenant name from setup (create_tenant produced none); nothing to list"
        print(json.dumps(result, indent=2))
        return 1

    tv_client = resourcemanager_v3.TagValuesClient()

    # Resolve the forwarded namespaced name to the permanent TagValue so we learn
    # its parent TagKey (list requires the parent) and its permanent id (for the
    # exact-match found_target signal).
    try:
        target = retry_idempotent(
            tv_client.get_namespaced_tag_value,
            name=args.group_name,
            op_desc="get_namespaced_tag_value",
        )
    except gax.NotFound:
        result["error"] = f"tenant '{args.group_name}' not found"
        print(json.dumps(result, indent=2))
        return 1

    # Materialize the full pager inside retry_idempotent_list so a transient on
    # ANY later-page fetch -- not just the first request -- is retried across the
    # full set of transient errors rather than escaping the retry envelope.
    tenants: list[dict[str, str]] = []
    for value in retry_idempotent_list(tv_client.list_tag_values, parent=target.parent, op_desc="list_tag_values"):
        tenants.append({"tenant_name": value.namespaced_name, "tenant_id": value.name})

    result["tenants"] = tenants
    result["count"] = len(tenants)
    result["found_target"] = any(t["tenant_id"] == target.name for t in tenants)
    result["success"] = True

    if not result["found_target"]:
        # The created tenant must appear under its own parent; a miss is a real
        # failure with a classified, visible diagnostic.
        result["success"] = False
        result["error"] = classify_gcp_error(
            RuntimeError(f"target tenant {target.name} not present in list under parent {target.parent}")
        )[1]

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
