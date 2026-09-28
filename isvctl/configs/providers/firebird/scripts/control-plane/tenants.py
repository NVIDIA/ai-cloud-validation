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

"""List the caller's tenants or read the run's tenant (CP08-01, CP09-01).

Tenants are not created by tenants (registration + operator verification), so
the run's own tenant is the target: the ``tenantId`` of the run's project.
``GET /users/me`` returns ``tenants[]`` (ID, name, type, compliance status).
An API that does not fill ``tenants[]`` for service accounts returns an empty
list; the step then skips. Run with a user token (``FIREBIRD_BEARER_TOKEN``) to
cover it on such an API.

Usage:
    python tenants.py --action list
    python tenants.py --action get

Output JSON (list):
{"success": true, "platform": "control_plane", "tenants": [{"tenant_id": "tenant.x", "tenant_name": "acme"}],
 "count": 1, "found_target": true, "target_tenant": "tenant.x"}
Output JSON (get):
{"success": true, "platform": "control_plane", "tenant_id": "tenant.x", "tenant_name": "acme",
 "description": "organization tenant, VERIFIED"}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log

SA_PREFIX = "service-account."
SA_SKIP_REASON = (
    "GET /users/me returned no tenants for this service account (the API does not list tenants for "
    "service-account principals); set FIREBIRD_BEARER_TOKEN to a user's token to cover tenant listing"
)


def main() -> int:
    """Read /users/me and the run's project; emit the tenant list or the run's tenant.

    Returns:
        0 on success or skip, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Firebird tenant list / info")
    parser.add_argument("--action", choices=("list", "get"), required=True, help="list tenants or get the run's")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}
    if args.action == "get":
        result.update(tenant_id="", tenant_name="")
    try:
        client = FirebirdClient()
        me = client.request("GET", "/users/me")
        tenants = [t for t in me.get("tenants") or [] if isinstance(t, dict)]
        if not tenants and str(me.get("userId", "")).startswith(SA_PREFIX):
            result.update(success=True, skipped=True, skip_reason=SA_SKIP_REASON)
            print(json.dumps(result, indent=2))
            return 0
        target = client.get_project()["tenantId"]
        match = next((t for t in tenants if t.get("id") == target), None)

        if args.action == "list":
            result["tenants"] = [{"tenant_id": t.get("id", ""), "tenant_name": t.get("name", "")} for t in tenants]
            result.update(count=len(tenants), found_target=match is not None, target_tenant=target)
            result["success"] = match is not None
            if match is None:
                result["error"] = f"tenant {target} of project {client.project_id} is not in /users/me tenants"
        elif match is None:
            result["error"] = f"tenant {target} of project {client.project_id} is not in /users/me tenants"
        else:
            result.update(tenant_id=target, tenant_name=match.get("name", ""))
            result["description"] = f"{match.get('tenantType', '')} tenant, {match.get('complianceStatus', '')}"
            result["success"] = bool(result["tenant_name"])
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
