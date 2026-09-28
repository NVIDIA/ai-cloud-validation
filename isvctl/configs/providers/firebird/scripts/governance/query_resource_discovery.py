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

"""Poll the tenant BM pool as the resource discovery index (CAP03-01).

The tenant-level BM listing (``GET /compute/bms``) is the index
of capacity delivered to the tenant, keyed by the permanent ``bm.ULID``. It is
polled --polls times so identifier stability is observed: an ID present in the
first poll and missing from the last is reported unstable (capacity appearing
mid-run is not).

A BM counts as discovered once its hardware is registered: ALLOCATED ("tenant
ownership recorded; hardware registration has not started") and PROVISIONING
(which also covers hardware registration and inspection) do not count. The API
states no delivery reason, so none is reported.

Usage:
    python query_resource_discovery.py [--polls 2] [--poll-interval 5]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_resource_discovery",
    "polls": 2,
    "poll_interval_seconds": 5,
    "unstable_identifiers": [],
    "resources_checked": 1,
    "resources": [{"resource_id": "bm.xxx", "discovered": true}]
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient

TEST_NAME = "query_resource_discovery"
UNREGISTERED_STATES = frozenset({"", "UNSPECIFIED", "ALLOCATED", "PROVISIONING"})


def main() -> int:
    """Poll the tenant BM pool and emit the resource discovery JSON contract.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Poll the Firebird tenant BM pool")
    parser.add_argument("--polls", type=int, default=2, help="How many times to poll the index")
    parser.add_argument("--poll-interval", type=int, default=5, help="Seconds between polls")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "test_name": TEST_NAME,
        "polls": 0,
        "poll_interval_seconds": args.poll_interval,
        "unstable_identifiers": [],
        "resources": [],
    }
    try:
        client = FirebirdClient()
        first: list[str] = []
        last: list[dict[str, Any]] = []
        for poll in range(max(1, args.polls)):
            if poll:
                time.sleep(args.poll_interval)
            last = client.paginate("/compute/bms", "items")
            result["polls"] = poll + 1
            if not poll:
                first = [bm.get("id") or "" for bm in last]

        last_ids = {bm.get("id") for bm in last}
        result["unstable_identifiers"] = [bm_id for bm_id in first if bm_id not in last_ids]
        result["resources"] = [
            {"resource_id": bm.get("id") or "", "discovered": (bm.get("state") or "") not in UNREGISTERED_STATES}
            for bm in last
        ]
        result["resources_checked"] = len(result["resources"])
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
