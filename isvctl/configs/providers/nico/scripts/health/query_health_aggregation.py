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

"""Aggregate host health to the nodegroup primitive for a NICo site (CAP05-02).

NICo's nodegroup primitive is the InstanceType: machines carry an
``instanceTypeId`` that groups them into a logical pool. This script rolls
per-host health up to that primitive, producing a per-group healthy/unhealthy
breakdown and an aggregate status the validation can check for internal
consistency. Machines with no instance type are grouped under ``unassigned``;
``Decommissioned`` machines are excluded from the live fleet.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/machine?siteId={site_id}

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_SSA_ISSUER,
    NICO_CLIENT_ID, NICO_CLIENT_SECRET, and optional NICO_OIDC_SCOPE.

A machine counts as unhealthy when its status is Error/Unknown or its health
report carries any alerts; otherwise it is healthy.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "aggregation_level": "nodegroup",
    "groups": [
      {
        "group_id": "...",
        "group_type": "instance_type",
        "name": "...",
        "total": 20,
        "healthy": 19,
        "unhealthy": 1,
        "status": "Degraded",
        "unhealthy_hosts": ["..."]
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> python query_health_aggregation.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    OpenAPI spec: rest-api/openapi/spec.yaml (Machine.instanceTypeId, MachineStatusBreakdown)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, classify_health, forge_get_all, resolve_auth

# The nodegroup primitive NICo aggregates health over.
AGGREGATION_LEVEL = "nodegroup"
GROUP_TYPE = "instance_type"
UNASSIGNED_GROUP = "unassigned"

# Machine statuses that are unhealthy regardless of the probe report.
UNHEALTHY_STATUSES: frozenset[str] = frozenset({"Error", "Unknown"})
# Machines in this status are no longer part of the live fleet.
EXCLUDED_STATUSES: frozenset[str] = frozenset({"Decommissioned"})


def _is_unhealthy(machine: dict[str, Any]) -> bool:
    """Return whether a machine is unhealthy by status or health alerts."""
    status = machine.get("status") or "Unknown"
    if status in UNHEALTHY_STATUSES:
        return True
    return classify_health(machine.get("health") or {}) == "unhealthy"


def aggregate_by_nodegroup(machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group machines by instance type and summarize per-group health."""
    groups: dict[str, dict[str, Any]] = {}

    for machine in machines:
        status = machine.get("status") or "Unknown"
        if status in EXCLUDED_STATUSES:
            continue

        group_id = machine.get("instanceTypeId") or UNASSIGNED_GROUP
        group = groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "group_type": GROUP_TYPE,
                "name": group_id,
                "total": 0,
                "healthy": 0,
                "unhealthy": 0,
                "status": "Healthy",
                "unhealthy_hosts": [],
            },
        )

        group["total"] += 1
        if _is_unhealthy(machine):
            group["unhealthy"] += 1
            group["unhealthy_hosts"].append(machine.get("id", ""))
        else:
            group["healthy"] += 1

    for group in groups.values():
        group["status"] = "Healthy" if group["unhealthy"] == 0 else "Degraded"

    # Stable ordering keeps the output deterministic across runs.
    return [groups[key] for key in sorted(groups)]


def main() -> int:
    """Aggregate NICo host health by nodegroup and print the JSON contract."""
    parser = argparse.ArgumentParser(description="Aggregate NICo host health by nodegroup")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "aggregation_level": AGGREGATION_LEVEL,
        "groups": [],
    }

    try:
        auth = resolve_auth()

        machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="machines",
        )

        result["groups"] = aggregate_by_nodegroup(machines)
        result["success"] = True

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
