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
  GET /v2/org/{org}/forge/machine?siteId={site_id}&includeMetadata=true

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_ISSUER_URL,
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
    NICO_BEARER_TOKEN=<token> python check_dpu_health.py --org <org> --site-id <uuid>

Reference:
    OpenAPI spec: ncp-isv-carbide-proxy-service/src/main/resources/docs/openapi/forge_api.yaml
"""

import argparse
import json
import sys
from pathlib import Path

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import DEFAULT_API_BASE, NicoAuthError, forge_get_all, resolve_auth, sum_capabilities

# Known DPU-related alert targets and probe IDs from the NICo API.
# The stub uses these for pre-filtering; the validation class should
# also check health_summary for a complete picture.
DPU_ALERT_TARGETS = {"forge-dpu-agent", "dpu"}
DPU_ALERT_IDS = {"heartbeattimeout", "dpudiskutilizationcheck"}


def _is_dpu_alert(alert: dict) -> bool:
    """Check if a health alert is DPU-related."""
    target = alert.get("target", "").lower()
    alert_id = alert.get("id", "").lower()
    return any(t in target for t in DPU_ALERT_TARGETS) or any(i in alert_id for i in DPU_ALERT_IDS)


def _has_dpu_heartbeat(health: dict) -> bool:
    """Check if DPU agent heartbeat is active (no HeartbeatTimeout alerts on DPU targets)."""
    for alert in health.get("alerts", []):
        target = alert.get("target", "").lower()
        alert_id = alert.get("id", "").lower()
        if "dpu" in target and "heartbeat" in alert_id:
            return False
    return True


def _extract_health_successes(health: dict) -> list[str]:
    """Extract health probe success IDs."""
    return [s.get("id", "") for s in health.get("successes", []) if s.get("id")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check DPU health on NICo machines")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="Forge site UUID")
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help="Forge API base URL (default: NGC production)",
    )
    args = parser.parse_args()

    result: dict = {
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
            params={"siteId": args.site_id, "includeMetadata": "true"},
            result_key="machines",
        )

        for machine in machines:
            machine_id = machine.get("id", "")
            metadata = machine.get("metadata", {})
            dmi = metadata.get("dmiData", {})
            chassis_serial = dmi.get("chassisSerial", machine_id)
            health = machine.get("health", {})
            capabilities = machine.get("machineCapabilities", [])

            # Count DPU capabilities (sum count field, not entries)
            dpu_count = sum_capabilities(capabilities, "DPU")

            # Build DPU capability summary
            dpu_caps = [c for c in capabilities if c.get("type") == "DPU"]
            dpu_capability = None
            if dpu_caps:
                first_dpu = dpu_caps[0]
                dpu_capability = {
                    "type": "DPU",
                    "name": first_dpu.get("name", "Unknown"),
                    "count": dpu_count,
                }

            # Extract health data
            health_successes = _extract_health_successes(health)
            all_alerts = health.get("alerts", [])
            dpu_alerts = [
                {"id": a.get("id", ""), "target": a.get("target", ""), "message": a.get("message", "")}
                for a in all_alerts
                if _is_dpu_alert(a)
            ]
            heartbeat = _has_dpu_heartbeat(health)

            # health_summary: unhealthy if ANY alerts (not just DPU-filtered ones)
            health_summary = "unhealthy" if all_alerts else "healthy"

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
