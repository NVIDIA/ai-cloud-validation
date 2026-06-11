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

"""Check DPU health for all machines at a NICo site.

Queries the NICo REST API for machine health data including DPU-specific
probes, agent heartbeat status, and capability inventory.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/machine?siteId={site_id}&type=DPU&includeMetadata=true

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_SSA_ISSUER,
    NICO_CLIENT_ID, NICO_CLIENT_SECRET, and optional NICO_OIDC_SCOPE.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "machines_checked": 2,
    "machines": [
      {
        "machine_id": "...",
        "chassis_serial": "...",
        "status": "Ready",
        "dpu_count": 2,
        "dpu_capability": {"type": "DPU", "name": "BlueField-3", "count": 2},
        "health_summary": "healthy",
        "health_successes": ["DpuDiskUtilizationCheck", "BgpDaemonEnabled"],
        "health_alerts": [],
        "dpu_agent_heartbeat": true
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> python check_dpu_health.py --org <org> --site-id <uuid> --api-base <url>

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

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, classify_health, forge_get_all, resolve_auth, sum_capabilities

# Known DPU-related alert targets and probe IDs from the NICo API.
# The stub uses these for pre-filtering; the validation class should
# also check health_summary for a complete picture.
DPU_ALERT_TARGETS = {"carbide-dpu-agent", "dpu"}
DPU_ALERT_IDS = {"heartbeattimeout", "dpudiskutilizationcheck"}


def _lower_field(data: dict[str, Any], field: str) -> str:
    """Return a lowercase string field, treating JSON null as missing."""
    value = data.get(field)
    return value.lower() if isinstance(value, str) else ""


def _is_dpu_alert(alert: dict[str, Any]) -> bool:
    """Check if a health alert is DPU-related."""
    target = _lower_field(alert, "target")
    alert_id = _lower_field(alert, "id")
    return any(t in target for t in DPU_ALERT_TARGETS) or any(i in alert_id for i in DPU_ALERT_IDS)


def _has_dpu_heartbeat(health: dict[str, Any]) -> bool:
    """Check if DPU agent heartbeat is active (no HeartbeatTimeout alerts on DPU targets)."""
    for alert in health.get("alerts") or []:
        target = _lower_field(alert, "target")
        alert_id = _lower_field(alert, "id")
        if "dpu" in target and "heartbeat" in alert_id:
            return False
    return True


def _extract_health_successes(health: dict[str, Any]) -> list[str]:
    """Extract health probe success IDs."""
    return [s.get("id", "") for s in health.get("successes") or [] if s.get("id")]


def main() -> int:
    """Query NICo machine health and print DPU health JSON to stdout."""
    parser = argparse.ArgumentParser(description="Check DPU health on NICo machines")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="Forge site UUID")
    parser.add_argument(
        "--api-base",
        required=True,
        help="Forge API base URL",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "machines_checked": 0,
        "machines": [],
    }

    try:
        auth = resolve_auth()

        # Fetch all machines with metadata (paginated)
        machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id, "type": "DPU", "includeMetadata": "true"},
            result_key="machines",
        )

        for machine in machines:
            machine_id = machine.get("id", "")
            health = machine.get("health") or {}
            capabilities = machine.get("machineCapabilities") or []
            # Real chassis serial, debug aid only (empty when metadata is absent;
            # never falls back to machine_id). Display/matching keys off machine_id.
            chassis_serial = ((machine.get("metadata") or {}).get("dmiData") or {}).get("chassisSerial", "")

            # Build DPU capability summary
            dpu_caps = [c for c in capabilities if c.get("type") == "DPU"]

            # The server-side type=DPU filter is ignored by API versions that
            # key off capabilityType, so non-DPU machines can come back in the
            # response. Skip them here so DPU health checks only run against
            # machines that actually have a DPU.
            if not dpu_caps:
                continue

            # Count DPU capabilities (sum count field, not entries)
            dpu_count = sum_capabilities(capabilities, "DPU")

            # dpu_caps is non-empty here -- machines without a DPU are skipped above.
            dpu_capability = {
                "type": "DPU",
                "name": dpu_caps[0].get("name", "Unknown"),
                "count": dpu_count,
            }

            # Extract health data
            health_successes = _extract_health_successes(health)
            all_alerts = health.get("alerts") or []
            dpu_alerts = [
                {"id": a.get("id", ""), "target": a.get("target", ""), "message": a.get("message", "")}
                for a in all_alerts
                if _is_dpu_alert(a)
            ]
            heartbeat = _has_dpu_heartbeat(health)

            # health_summary covers ANY alerts, not just the DPU-filtered ones above.
            health_summary = classify_health(health)

            result["machines"].append(
                {
                    "machine_id": machine_id,
                    "chassis_serial": chassis_serial,
                    "status": machine.get("status", "Unknown"),
                    "dpu_count": dpu_count,
                    "dpu_capability": dpu_capability,
                    "health_summary": health_summary,
                    "health_successes": health_successes,
                    "health_alerts": dpu_alerts,
                    "dpu_agent_heartbeat": heartbeat,
                }
            )

        result["machines_checked"] = len(result["machines"])
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
