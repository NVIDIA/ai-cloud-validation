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

"""Per-node fleet inventory from the NCP fleet-capacity report (CAP02-01).

Maps each fleet-capacity row onto the CAP02 per-node record. The fields map
one to one (``cspAccount`` is the account ID); ``healthState`` is lowercased,
so a node the report classifies UNKNOWN stays "unknown" and fails the check
rather than being guessed healthy. Unset fields are passed through empty.

Scope: the calling tenant's nodes only. The report is optional; skips with a
structured skip when the route answers 404 or 501.

Usage:
    python query_fleet_inventory.py

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_fleet_inventory",
    "nodes_checked": 1,
    "nodes": [{
        "node_id": "bm.xxx", "health_state": "healthy", "instance_id": "bm.xxx",
        "created_at": "2026-01-01T00:00:00Z", "hardware_type": "H100", "gpu_count": 8,
        "account_id": "tenant.xxx", "project_id": "project.xxx", "in_use": true, "region": "region-1"
    }]
}
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient
from common.fleet_capacity import fleet_capacity_rows, mark_skipped

TEST_NAME = "query_fleet_inventory"


def _text(row: dict[str, Any], field: str) -> str:
    """Return a string field, or "" when unset."""
    value = row.get(field)
    return value if isinstance(value, str) else ""


def to_node(row: dict[str, Any]) -> dict[str, Any]:
    """Map one fleet-capacity row onto the CAP02 node record."""
    return {
        "node_id": _text(row, "nodeId"),
        "health_state": _text(row, "healthState").lower(),
        "instance_id": _text(row, "instanceId"),
        "created_at": _text(row, "createdAt"),
        "hardware_type": _text(row, "hardwareType"),
        "gpu_count": int(row.get("gpuCount") or 0),
        "account_id": _text(row, "cspAccount"),
        "project_id": _text(row, "projectId"),
        "in_use": row.get("inUse") is True,
        "region": _text(row, "region"),
    }


def main() -> int:
    """Emit the fleet inventory JSON contract.

    Returns:
        0 on success or skip, 1 on failure
    """
    result: dict[str, Any] = {"success": False, "platform": "bm", "test_name": TEST_NAME}
    try:
        rows = fleet_capacity_rows(FirebirdClient())
        if rows is None:
            mark_skipped(result)
        else:
            result["nodes"] = [to_node(row) for row in rows]
            result["nodes_checked"] = len(result["nodes"])
            result["success"] = True
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
