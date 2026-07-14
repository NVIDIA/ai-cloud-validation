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

NICo exposes host health as an alert-driven report (``health.successes`` and
``health.alerts``): a healthy subsystem is simply the absence of an alert, so a
passing per-category probe is not guaranteed. Each probe has a stable ``id``
(e.g. ``BmcSensor``, ``BmcLeakDetection``, ``HeartbeatTimeout``), an optional
``target`` component, and ``classifications`` (e.g. ``SensorCritical``,
``Leak``). This script reports, per host, the probe IDs the API returned, every
alert with its classifications, and the observation freshness, so the
validation can assert the per-host health API works and the host is healthy.

It additionally emits an *informational* component breakdown (GPU / thermal /
memory / cooling) derived from probe targets -- NICo folds a GPU/DIMM
temperature into a ``BmcSensor`` whose ``target`` names the component, and
liquid-cooling leaks into ``BmcLeakDetection`` -- but the validation does not
gate on this breakdown.

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
        "health_present": true,
        "healthy": true,
        "observed_age_seconds": 12,
        "probe_ids": ["BmcSensor", "BgpDaemonEnabled"],
        "alerts": [
          {"id": "BmcLeakDetection", "target": "RackLeakDetector_1",
           "message": "...", "classifications": ["Leak"]}
        ],
        "components": {
          "gpu":     {"present": true, "alerting": false, "probes": ["BmcSensor"]},
          "thermal": {"present": true, "alerting": false, "probes": ["BmcSensor"]},
          "memory":  {"present": false, "alerting": false, "probes": []},
          "cooling": {"present": true, "alerting": false, "probes": ["BmcLeakDetection"]}
        }
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> python query_host_health.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    Probe IDs:        infra-controller docs/architecture/health/health_probe_ids.md
    Classifications:  infra-controller docs/architecture/health/health_alert_classifications.md
    OpenAPI spec:     rest-api/openapi/spec.yaml (HealthReport / HealthProbe* schemas)
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, probe_text, resolve_auth

# Substring keywords that map a NICo health probe (by id/target/message) into an
# informational hardware component bucket. NICo does not model GPU/thermal/memory
# as first-class probes -- a GPU or DIMM temperature surfaces as a ``BmcSensor``
# whose target names the component, and a coolant leak as ``BmcLeakDetection`` --
# so this breakdown is best-effort and for visibility only. The validation gates
# on probe IDs, alerts, and classifications, not on these buckets.
COMPONENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gpu": ("gpu", "nvlink", "nvswitch", "nvml", "xid", "vbios", "vgpu"),
    "thermal": ("temp", "thermal", "fan"),
    "memory": ("memory", "ecc", "dimm", "rowremap", "row_remap", "hbm", "sram"),
    "cooling": ("leak", "coolant", "cooling", "cdu", "liquid", "water"),
}


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    """Check whether precomputed probe text contains any component keyword."""
    return any(keyword in text for keyword in keywords)


def _classifications(probe: dict[str, Any]) -> list[str]:
    """Return a probe's classification strings, tolerating null/missing values."""
    return [c for c in (probe.get("classifications") or []) if isinstance(c, str)]


def summarize_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Reduce a NICo health alert to the provider-neutral fields validations use."""
    return {
        "id": alert.get("id", ""),
        "target": alert.get("target", ""),
        "message": alert.get("message", ""),
        "classifications": _classifications(alert),
    }


def component_breakdown(health: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a NICo health report into an informational per-component summary."""
    successes = health.get("successes") or []
    alerts = health.get("alerts") or []

    # Compute each probe's match text once, then reuse it across every component
    # rather than rebuilding the lowercased string per (probe, component) pair.
    success_texts = [(s, probe_text(s)) for s in successes]
    alert_texts = [(a, probe_text(a)) for a in alerts]

    components: dict[str, dict[str, Any]] = {}
    for component, keywords in COMPONENT_KEYWORDS.items():
        probe_ids = sorted(
            {
                s.get("id", "")
                for s, text in (*success_texts, *alert_texts)
                if _matches_keywords(text, keywords) and s.get("id")
            }
        )
        alerting = any(_matches_keywords(text, keywords) for _, text in alert_texts)
        components[component] = {
            "present": bool(probe_ids),
            "alerting": alerting,
            "probes": probe_ids,
        }
    return components


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


def host_health(machine: dict[str, Any]) -> dict[str, Any]:
    """Build the per-host health record from a NICo machine payload."""
    health = machine.get("health") or {}
    successes = health.get("successes") or []
    alerts = health.get("alerts") or []
    chassis_serial = ((machine.get("metadata") or {}).get("dmiData") or {}).get("chassisSerial", "")

    probe_ids = sorted({p.get("id", "") for p in (*successes, *alerts) if p.get("id")})
    # The health API returned a report for this host if it carries any probe data
    # or an observation timestamp (NICo only lists alerts on failure, so a healthy
    # host can have an empty successes list).
    health_present = bool(successes or alerts or health.get("observedAt"))

    return {
        "host_id": machine.get("id", ""),
        "chassis_serial": chassis_serial,
        "status": machine.get("status", "Unknown"),
        "health_present": health_present,
        "healthy": len(alerts) == 0,
        "observed_age_seconds": observed_age_seconds(health),
        "probe_ids": probe_ids,
        "alerts": [summarize_alert(a) for a in alerts],
        "components": component_breakdown(health),
    }


def main() -> int:
    """Query NICo machine health and print per-host health JSON to stdout."""
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

        result["hosts"] = [host_health(machine) for machine in machines]
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
