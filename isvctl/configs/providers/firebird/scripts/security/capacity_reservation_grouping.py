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

"""Capacity grouped and pinned to the tenant (CAP04-01).

Firebird has no reservation object: the operator pre-allocates bare-metal
servers to a tenant, and that tenant pool is the reservation. So
``reservation_id`` and ``account_id`` are both the run's tenant (the ``tenantId``
of its project), and the grouped resources are the tenant pool
(``GET /compute/bms``). A BM counts as pinned when it carries that tenant ID,
and isolation holds when every BM in the pool does.

The listing is filtered by tenant on the server, so ``isolation_enforced`` is
the API's tenant filter, not an observed property: another tenant's BMs are
never visible to check against - ``cross_tenant_observable`` is false to say so.

Usage:
    python capacity_reservation_grouping.py

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "capacity_reservation_grouping",
    "reservation_id": "tenant.xxx",
    "account_id": "tenant.xxx",
    "pinned": true,
    "isolation_enforced": true,
    "cross_tenant_observable": false,
    "resources": [{"resource_id": "bm.xxx", "resource_type": "bare_metal", "account_id": "tenant.xxx", "pinned": true}]
}
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log


def main() -> int:
    """List the tenant BM pool and emit it as the tenant's reservation.

    Returns:
        0 on success, 1 on failure
    """
    result: dict[str, Any] = {"success": False, "platform": "security", "test_name": "capacity_reservation_grouping"}
    try:
        client = FirebirdClient()
        tenant = client.get_project()["tenantId"]
        pool = client.paginate("/compute/bms", "items")
        resources = [
            {
                "resource_id": bm.get("id", ""),
                "resource_type": "bare_metal",
                "account_id": bm.get("tenantId", ""),
                "pinned": bm.get("tenantId") == tenant,
            }
            for bm in pool
        ]
        pinned = bool(resources) and all(r["pinned"] for r in resources)
        result.update(
            reservation_id=tenant,
            account_id=tenant,
            resources=resources,
            pinned=pinned,
            isolation_enforced=pinned,
            # The pool is server-filtered to this tenant; other tenants' BMs are not observable.
            cross_tenant_observable=False,
            success=True,
        )
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
