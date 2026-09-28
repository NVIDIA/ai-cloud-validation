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

"""Governance capacity metrics from the NCP fleet-capacity report (CAP01-01).

Rolls the tenant's fleet-capacity rows up into the four governance buckets,
for nodes and GPUs (``gpuCount``). The report derives each row's
``capacityState`` most-specific first (In Use > Reserved > Healthy >
Delivered), where In Use means the node runs a workload and Reserved means it
is committed to a CSP account but idle. Bucket mapping:
  Delivered: every row (onboarded hardware made available to the tenant)
  Healthy:   healthState HEALTHY - read from healthState, not capacityState,
             because capacityState reports "Healthy" only for idle,
             unattributed nodes and would under-count
  Reserved:  capacityState In Use or Reserved (committed to an account)
  Active:    capacityState In Use (running a tenant workload)
so Active <= Reserved <= Delivered and Healthy <= Delivered by construction.

Scope: the report returns only the calling tenant's nodes, so these are the
tenant's metrics, not site-wide totals. The report is optional; skips with a
structured skip when the route answers 404 or 501.

Usage:
    python query_governance_metrics.py

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_governance_metrics",
    "node_count": 2,
    "metrics": {
        "delivered": {"nodes": 2, "gpus": 16},
        "healthy":   {"nodes": 2, "gpus": 16},
        "reserved":  {"nodes": 2, "gpus": 16},
        "active":    {"nodes": 1, "gpus": 8}
    }
}
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient
from common.fleet_capacity import fleet_capacity_rows, mark_skipped

TEST_NAME = "query_governance_metrics"
METRIC_BUCKETS = ("delivered", "healthy", "reserved", "active")
RESERVED_STATES = frozenset({"In Use", "Reserved"})
ACTIVE_STATES = frozenset({"In Use"})


def aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Sum node and GPU counts per governance bucket."""
    metrics = {bucket: {"nodes": 0, "gpus": 0} for bucket in METRIC_BUCKETS}
    for row in rows:
        state = row.get("capacityState") or ""
        membership = {
            "delivered": True,
            "healthy": row.get("healthState") == "HEALTHY",
            "reserved": state in RESERVED_STATES,
            "active": state in ACTIVE_STATES,
        }
        gpus = max(0, int(row.get("gpuCount") or 0))
        for bucket, member in membership.items():
            if member:
                metrics[bucket]["nodes"] += 1
                metrics[bucket]["gpus"] += gpus
    return metrics


def main() -> int:
    """Emit the governance metrics JSON contract.

    Returns:
        0 on success or skip, 1 on failure
    """
    result: dict[str, Any] = {"success": False, "platform": "bm", "test_name": TEST_NAME}
    try:
        rows = fleet_capacity_rows(FirebirdClient())
        if rows is None:
            mark_skipped(result)
        else:
            result["node_count"] = len(rows)
            result["metrics"] = aggregate_metrics(rows)
            result["success"] = True
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
