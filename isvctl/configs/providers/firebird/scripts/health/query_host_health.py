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

"""Per-host health from the NCP fleet-capacity report (CAP05-01).

The report carries one coarse health classification per node (``healthState``
HEALTHY | UNHEALTHY | UNKNOWN) and when it was observed
(``healthObservedAt``). It exposes no probe IDs or per-component alerts, so:
  - health_present: the node was classified (HEALTHY or UNHEALTHY); UNKNOWN
    means no health report
  - healthy / alerts: an UNHEALTHY node carries one alert standing for that
    classification - otherwise the check, which fails only on alerts, would
    pass an unhealthy node. No probe or component detail is invented.
  - observed_age_seconds: age of healthObservedAt, or None when unset
  - probe_ids: always empty

Scope: the calling tenant's nodes only. The report is optional; skips with a
structured skip when the route answers 404 or 501.

Usage:
    python query_host_health.py

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_host_health",
    "hosts_checked": 1,
    "hosts": [{
        "host_id": "bm.xxx", "health_present": true, "healthy": true,
        "observed_age_seconds": 120, "probe_ids": [], "alerts": []
    }]
}
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, parse_timestamp
from common.fleet_capacity import fleet_capacity_rows, mark_skipped

TEST_NAME = "query_host_health"
CLASSIFIED_STATES = frozenset({"HEALTHY", "UNHEALTHY"})


def to_host(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Map one fleet-capacity row onto the CAP05-01 host record."""
    host_id = row.get("nodeId") or row.get("instanceId") or ""
    state = row.get("healthState") or ""
    observed = parse_timestamp(row.get("healthObservedAt"))
    alerts = []
    if state == "UNHEALTHY":
        alerts.append(
            {
                "id": "health_state",
                "target": host_id,
                "message": "fleet-capacity report classifies the node UNHEALTHY",
                "classifications": ["Unhealthy"],
            }
        )
    return {
        "host_id": host_id,
        "health_present": state in CLASSIFIED_STATES,
        "healthy": state == "HEALTHY",
        "observed_age_seconds": max(0, int((now - observed).total_seconds())) if observed else None,
        "probe_ids": [],
        "alerts": alerts,
    }


def main() -> int:
    """Emit the host health JSON contract.

    Returns:
        0 on success or skip, 1 on failure
    """
    result: dict[str, Any] = {"success": False, "platform": "bm", "test_name": TEST_NAME}
    try:
        rows = fleet_capacity_rows(FirebirdClient())
        if rows is None:
            mark_skipped(result)
        else:
            now = datetime.now(UTC)
            result["hosts"] = [to_host(row, now) for row in rows]
            result["hosts_checked"] = len(result["hosts"])
            result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
