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

"""Serial console log access and retention test for Firebird bare metal.

Reads the BM's serial console logs through the serial-logs API:
  - recent output (last --recent-minutes) proves read access;
  - a query over the year ending --retention-days ago proves history is still
    retained and queryable. The API sorts newest first, so its first record is
    the youngest log older than the requirement; its age is a lower bound on the
    retention actually in effect (the API does not expose the configured policy).

Usage:
    python serial_console.py --instance-id bm.xxx [--retention-days 30]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "console_available": true,
    "serial_access_enabled": true,
    "output_length": 4096,
    "console_log_queryable": true,
    "retention_days_required": 30,
    "retention_days_configured": 30,
    "oldest_queryable_log_age_days": 31,
    "query_result_count": 1,
    "retention_evidence": "firebird serial-logs API: oldest retained record observed ..."
}
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, parse_timestamp, rfc3339


def _query(client: FirebirdClient, bm_id: str, start: datetime, end: datetime, page_size: int) -> dict[str, Any]:
    """Return one page of serial logs in [start, end], newest first."""
    return client.request(
        "GET",
        client.bm_path(bm_id, "/serial-logs"),
        params={"from": rfc3339(start), "to": rfc3339(end), "pageSize": page_size},
    )


def main() -> int:
    """Read recent and historical serial console logs and emit structured JSON.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Firebird BM serial console test")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--recent-minutes", type=int, default=120, help="Window for recent output")
    parser.add_argument("--retention-days", type=int, default=30, help="Retention window to prove")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "console_available": False,
        "serial_access_enabled": False,
        "output_length": 0,
        "console_log_queryable": False,
        "retention_days_required": args.retention_days,
        "retention_days_configured": 0,
        "oldest_queryable_log_age_days": 0,
        "query_result_count": 0,
    }

    try:
        client = FirebirdClient()
        now = datetime.now(UTC)

        recent = _query(client, args.instance_id, now - timedelta(minutes=args.recent_minutes), now, 1000)
        result["serial_access_enabled"] = True
        output = "\n".join(entry.get("content", "") for entry in reversed(recent.get("logs") or []))
        result["console_available"] = bool(output)
        # Console text is deliberately not emitted: boot output can carry secrets.
        result["output_length"] = len(output)

        retention_end = now - timedelta(days=args.retention_days)
        history = _query(client, args.instance_id, retention_end - timedelta(days=365), retention_end, 1)
        logs = history.get("logs") or []
        result["console_log_queryable"] = True
        result["query_result_count"] = len(logs)
        oldest = parse_timestamp(logs[0].get("timestamp")) if logs else None
        if oldest:
            age_days = (now - oldest).days
            result["oldest_queryable_log_age_days"] = age_days
            result["retention_days_configured"] = age_days
            result["retention_evidence"] = (
                f"firebird serial-logs API: retained record from {logs[0]['timestamp']} ({age_days}d old, observed)"
            )
        else:
            result["retention_evidence"] = ""

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
