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

"""Aggregate site-wide governance metrics for the bare-metal fleet (CAP01-01).

Queries the NICo REST API for every machine at a site and aggregates them
into the four governance buckets a Cloud Governance API must surface:
Delivered, Healthy, Reserved, and Active. Counts are produced for both nodes
and GPUs so the validation can verify the API contract end-to-end.

NICo MachineStatus enum (per the upstream OpenAPI spec) used here:
  Initializing | Ready | Reset | Maintenance | InUse | Error | Decommissioned | Unknown

Bucket mapping for NICo:
  Delivered: status NOT in {Decommissioned, Unknown} -- machines NICo
             currently manages with a known state.
  Healthy:   subset of Delivered with no entries under ``health.alerts``.
  Reserved:  status in {InUse, Maintenance} -- machines committed to a tenant
             (in-use or held).
  Active:    status in {InUse} -- machines currently running a tenant workload.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/machine?siteId={site_id}

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_SSA_ISSUER,
    NICO_CLIENT_ID, NICO_CLIENT_SECRET, and optional NICO_OIDC_SCOPE.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "machine_count": 20,
    "metrics": {
      "delivered": {"nodes": 20, "gpus": 160},
      "healthy":   {"nodes": 19, "gpus": 152},
      "reserved":  {"nodes": 15, "gpus": 120},
      "active":    {"nodes": 10, "gpus":  80}
    }
  }

Usage:
    NICO_BEARER_TOKEN=<token> python query_metrics.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    OpenAPI spec: ncp-isv-carbide-proxy-service/src/main/resources/docs/openapi/forge_api.yaml
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, classify_health, forge_get_all, resolve_auth, sum_capabilities

# Machine status sets used to classify each machine into governance buckets.
# Sourced from MachineStatus in the upstream NICo OpenAPI spec.
DELIVERED_EXCLUDE_STATUSES: frozenset[str] = frozenset({"Decommissioned", "Unknown"})
RESERVED_STATUSES: frozenset[str] = frozenset({"InUse", "Maintenance"})
ACTIVE_STATUSES: frozenset[str] = frozenset({"InUse"})

# Empty metric template so every bucket appears in the output (even when zero)
# and the validation does not have to special-case missing keys.
_EMPTY_BUCKET: dict[str, int] = {"nodes": 0, "gpus": 0}


def _empty_metrics() -> dict[str, dict[str, int]]:
    """Return a fresh zeroed metrics dict with all four required buckets."""
    return {
        "delivered": dict(_EMPTY_BUCKET),
        "healthy": dict(_EMPTY_BUCKET),
        "reserved": dict(_EMPTY_BUCKET),
        "active": dict(_EMPTY_BUCKET),
    }


def _add(bucket: dict[str, int], nodes: int, gpus: int) -> None:
    """Accumulate node and GPU counts into ``bucket`` in place."""
    bucket["nodes"] += nodes
    bucket["gpus"] += gpus


def aggregate_metrics(machines: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Classify NICo machines into the four governance buckets and sum counts."""
    metrics = _empty_metrics()

    for machine in machines:
        status = machine.get("status") or "Unknown"
        capabilities = machine.get("machineCapabilities") or []
        gpu_count = sum_capabilities(capabilities, "GPU")
        health = machine.get("health") or {}

        if status in DELIVERED_EXCLUDE_STATUSES:
            # Decommissioned/Unknown machines are ignored entirely so they
            # cannot leak into Reserved or Active via a sloppy status string.
            continue

        _add(metrics["delivered"], nodes=1, gpus=gpu_count)

        if classify_health(health) == "healthy":
            _add(metrics["healthy"], nodes=1, gpus=gpu_count)

        if status in RESERVED_STATUSES:
            _add(metrics["reserved"], nodes=1, gpus=gpu_count)

        if status in ACTIVE_STATUSES:
            _add(metrics["active"], nodes=1, gpus=gpu_count)

    return metrics


def main() -> int:
    """Aggregate site-level governance metrics and print the JSON contract."""
    parser = argparse.ArgumentParser(description="Aggregate NICo site governance metrics")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "machine_count": 0,
        "metrics": _empty_metrics(),
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

        result["metrics"] = aggregate_metrics(machines)
        result["machine_count"] = len(machines)
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
