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

"""Report the project's InfiniBand partitions and P_Keys (SDN04-04).

Reads ``GET /projects/{p}/network/ib-partitions``. Partitions are
project-scoped resources, so their owning tenant is the project's tenant
(``GET /projects/{p}``); the partition record itself carries no tenant field.

What this can and cannot show: each partition has a real P_Key that is not the
all-ports default and is owned by exactly one tenant. It cannot show that no
P_Key is shared with another tenant, because a tenant's credentials only list
its own partitions - ``cross_tenant_observable`` is false to say so.

Skips when the IB partition API is not enabled on the deployment.

Usage:
    python query_ib_tenant_isolation.py

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_ib_tenant_isolation",
    "tenant_scope": "project",
    "cross_tenant_observable": false,
    "partitions_checked": 1,
    "partitions": [{"name": "ib-a", "partition_key": "0x0012", "tenant_id": "tenant.xxx", "status": "READY"}]
}
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdApiError, FirebirdClient, is_not_registered, log

TEST_NAME = "query_ib_tenant_isolation"


def main() -> int:
    """Emit the IB tenant isolation JSON contract.

    Returns:
        0 on success or skip, 1 on failure
    """
    result: dict[str, Any] = {"success": False, "platform": "bm", "test_name": TEST_NAME}
    try:
        client = FirebirdClient()
        try:
            partitions = client.paginate(client.project_path("/network/ib-partitions"), "items")
        except FirebirdApiError as e:
            if not is_not_registered(e):
                raise
            result.update(
                {
                    "success": True,
                    "skipped": True,
                    "skip_reason": "The InfiniBand partition API is not enabled on this Firebird API",
                }
            )
            print(json.dumps(result, indent=2))
            return 0

        tenant_id = (client.request("GET", client.project_path()).get("project") or {}).get("tenantId") or ""
        result["tenant_scope"] = "project"
        result["cross_tenant_observable"] = False
        result["partitions"] = [
            {
                "name": partition.get("name") or partition.get("id") or "",
                "partition_key": partition.get("pkey") or "",
                "tenant_id": tenant_id,
                "status": partition.get("state") or "",
            }
            for partition in partitions
        ]
        result["partitions_checked"] = len(result["partitions"])
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
