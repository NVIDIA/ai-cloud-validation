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

"""Query per-BM repair history from maintenance events (BFX02-03).

Reads ``GET /maintenance-events?status=ALL`` - open and cleared events - and
groups them by BM, oldest first, as each BM's repair history. --instance-id
narrows the query to one BM (``bmId``). The NCP repair report
(``/reports/ncp/repair``) is not used: its rows carry no node ID, so they
cannot be attributed to a node.

Usage:
    python query_repair_history.py [--instance-id bm.xxx]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_repair_history",
    "history_queryable": true,
    "records": [{
        "machine_id": "bm.xxx",
        "entries": [{"event_id": "maintenance-event.xxx", "status": "CLEARED",
                     "created_at": "2026-01-01T00:00:00Z", "cleared_at": "2026-01-02T00:00:00Z"}]
    }]
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log

TEST_NAME = "query_repair_history"


def main() -> int:
    """Emit the repair history JSON contract.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Query Firebird repair history")
    parser.add_argument("--instance-id", default="", help="Only history for this BM (bm.ULID)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "test_name": TEST_NAME,
        "history_queryable": False,
        "records": [],
    }
    try:
        events = FirebirdClient().paginate(
            "/maintenance-events", "items", params={"status": "ALL", "bmId": args.instance_id}
        )
        result["history_queryable"] = True
        by_bm: dict[str, list[dict[str, Any]]] = {}
        for event in sorted(events, key=lambda e: e.get("createdAt") or ""):
            by_bm.setdefault(event.get("bmId") or "", []).append(
                {
                    "event_id": event.get("id") or "",
                    "status": event.get("status") or "",
                    "created_at": event.get("createdAt") or "",
                    "cleared_at": event.get("clearedAt") or "",
                }
            )
        result["records"] = [{"machine_id": bm_id, "entries": entries} for bm_id, entries in by_bm.items()]
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
