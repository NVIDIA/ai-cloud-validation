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

"""Query current and upcoming maintenance events for the tenant's BMs (BFX02-01).

Reads ``GET /maintenance-events`` with the API's default status filter, OPEN
(ACTIVE + REQUESTED): the events still underway or awaiting the tenant.
--instance-id narrows the query to one BM (``bmId``). With no open events the
validation skips, since an empty list cannot demonstrate the API.

Usage:
    python query_maintenance_events.py [--instance-id bm.xxx]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_maintenance_events",
    "events_queryable": true,
    "events": [{"machine_id": "bm.xxx", "status": "ACTIVE", "message": "GPU fault"}]
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log

TEST_NAME = "query_maintenance_events"


def main() -> int:
    """Emit the maintenance events JSON contract.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Query Firebird maintenance events")
    parser.add_argument("--instance-id", default="", help="Only events for this BM (bm.ULID)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "test_name": TEST_NAME,
        "events_queryable": False,
        "events": [],
    }
    try:
        events = FirebirdClient().paginate("/maintenance-events", "items", params={"bmId": args.instance_id})
        result["events_queryable"] = True
        result["events"] = [
            {
                "machine_id": event.get("bmId") or "",
                "status": event.get("status") or "",
                "message": event.get("comment") or event.get("code") or "",
            }
            for event in events
        ]
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
