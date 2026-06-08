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

"""Query per-host health for all machines at a NICo site (CAP05-01).

NICo exposes host health as a probe report (``health.successes`` and
``health.alerts``), where each probe has a stable ``id`` and an optional
``target`` component. This script maps those probes into the GPU-state,
thermal, and memory-health categories NVIDIA requires a per-host health API to
surface, and reports the freshness of the observation so the validation can
confirm the data is real-time.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/machine?siteId={site_id}&includeMetadata=true

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_SSA_ISSUER,
    NICO_CLIENT_ID, NICO_CLIENT_SECRET, and optional NICO_OIDC_SCOPE.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "hosts_checked": 1,
    "hosts": [
      {
        "host_id": "...",
        "chassis_serial": "...",
        "status": "Ready",
        "observed_age_seconds": 12,
        "categories": {
          "gpu":     {"present": true, "healthy": true,  "probes": ["GpuRemappedRows"], "alerts": []},
          "thermal": {"present": true, "healthy": true,  "probes": ["Temperature"],     "alerts": []},
          "memory":  {"present": true, "healthy": false, "probes": [], "alerts": [{"id": "MemoryEcc", "message": "..."}]}
        }
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> python query_host_health.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    OpenAPI spec: rest-api/openapi/spec.yaml (HealthReport / HealthProbe* schemas)
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, resolve_auth

# Substring keywords that map a NICo health probe (by id or target) into one of
# the categories a per-host health API must surface for CAP05-01. Lowercased
# comparison; a probe matches a category if any keyword appears in its id or target.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gpu": ("gpu", "nvlink", "nvswitch", "nvml", "xid", "vbios", "vgpu"),
    "thermal": ("temp", "thermal", "fan", "coolant", "cooling"),
    "memory": ("memory", "ecc", "dimm", "rowremap", "row_remap", "hbm", "sram"),
}


def _probe_text(probe: dict[str, Any]) -> str:
    """Return the lowercased ``id`` + ``target`` + ``message`` text for matching.

    NICo reports BMC sensors under a single ``BmcSensor`` probe id and carries
    the sensor identity in ``target`` and the entity type in ``message`` (e.g.
    ``power_supply``, ``temperature``), so all three fields are searched.
    """
    parts = [probe.get("id"), probe.get("target"), probe.get("message")]
    return " ".join(p for p in parts if isinstance(p, str)).lower()


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    """Check whether precomputed probe text contains any category keyword."""
    return any(keyword in text for keyword in keywords)


def categorize_health(health: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a NICo health report into per-category presence/health summaries."""
    successes = health.get("successes") or []
    alerts = health.get("alerts") or []

    # Compute each probe's match text once, then reuse it across every category
    # rather than rebuilding the lowercased string per (probe, category) pair.
    success_texts = [(s, _probe_text(s)) for s in successes]
    alert_texts = [(a, _probe_text(a)) for a in alerts]

    categories: dict[str, dict[str, Any]] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        probe_ids = [s.get("id", "") for s, text in success_texts if _matches_keywords(text, keywords) and s.get("id")]
        cat_alerts = [
            {"id": a.get("id", ""), "target": a.get("target", ""), "message": a.get("message", "")}
            for a, text in alert_texts
            if _matches_keywords(text, keywords)
        ]
        categories[category] = {
            "present": bool(probe_ids or cat_alerts),
            "healthy": len(cat_alerts) == 0,
            "probes": probe_ids,
            "alerts": cat_alerts,
        }
    return categories


def observed_age_seconds(health: dict[str, Any], *, now: datetime | None = None) -> int | None:
    """Return the age in seconds of the health observation, or None if unknown.

    NICo timestamps are RFC 3339 / ISO 8601 (e.g. ``2019-08-24T14:15:22Z``).
    A missing or unparseable timestamp yields None so the validation can decide
    how strict to be about freshness.
    """
    observed_at = health.get("observedAt")
    if not isinstance(observed_at, str) or not observed_at:
        return None
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0, int((reference - parsed).total_seconds()))


def main() -> int:
    """Query NICo machine health and print per-host category health JSON."""
    parser = argparse.ArgumentParser(description="Query per-host health on NICo machines")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "hosts_checked": 0,
        "hosts": [],
    }

    try:
        auth = resolve_auth()

        machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id, "includeMetadata": "true"},
            result_key="machines",
        )

        for machine in machines:
            health = machine.get("health") or {}
            chassis_serial = ((machine.get("metadata") or {}).get("dmiData") or {}).get("chassisSerial", "")
            # The health API returned a report for this host if it carries any
            # probe data or an observation timestamp (NICo only lists alerts on
            # failure, so a healthy host can have an empty successes list).
            health_present = bool(
                (health.get("successes") or []) or (health.get("alerts") or []) or health.get("observedAt")
            )
            result["hosts"].append(
                {
                    "host_id": machine.get("id", ""),
                    "chassis_serial": chassis_serial,
                    "status": machine.get("status", "Unknown"),
                    "health_present": health_present,
                    "observed_age_seconds": observed_age_seconds(health),
                    "categories": categorize_health(health),
                }
            )

        result["hosts_checked"] = len(result["hosts"])
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
