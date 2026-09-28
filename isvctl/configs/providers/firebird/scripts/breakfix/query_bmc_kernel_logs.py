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

"""Query a BM's log history over a time window (BFX03-03).

Asks the serial-logs API (``GET /projects/{p}/compute/bms/{bm}/serial-logs``
with ``from``/``to``) for the last --window-hours of each host's console log,
and reports the window asked about and how many entries came back (one page,
at most --page-size). Log content is not emitted: boot output can carry
secrets.

Usage:
    python query_bmc_kernel_logs.py --instance-id bm.xxx [--instance-id bm.yyy] [--window-hours 24]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_bmc_kernel_logs",
    "hosts": [{
        "host_id": "bm.xxx", "window_start": "2026-01-01T00:00:00Z",
        "window_end": "2026-01-02T00:00:00Z", "entries_returned": 1000
    }]
}
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, rfc3339

TEST_NAME = "query_bmc_kernel_logs"


def main() -> int:
    """Query each host's log window and emit the BFX03-03 JSON contract.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Query Firebird BM log history")
    parser.add_argument("--instance-id", action="append", required=True, help="BM ID (bm.ULID); repeatable")
    parser.add_argument("--window-hours", type=int, default=24, help="Length of the queried window")
    parser.add_argument("--page-size", type=int, default=1000, help="Entries requested per host (API max 1000)")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bm", "test_name": TEST_NAME, "hosts": []}
    try:
        client = FirebirdClient()
        window_end = datetime.now(UTC)
        window_start = window_end - timedelta(hours=args.window_hours)
        start, end = rfc3339(window_start), rfc3339(window_end)
        for bm_id in args.instance_id:
            page = client.request(
                "GET",
                client.bm_path(bm_id, "/serial-logs"),
                params={"from": start, "to": end, "pageSize": args.page_size},
            )
            result["hosts"].append(
                {
                    "host_id": bm_id,
                    "window_start": start,
                    "window_end": end,
                    "entries_returned": len(page.get("logs") or []),
                }
            )
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
