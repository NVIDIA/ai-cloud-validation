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

"""Verify hardware ingestion against NICo expected-machine manifest.

Calls the NICo REST API to compare expected-machine records against
actually discovered machines. Each expected-machine record carries the
authoritative link to its discovered machine via the ``machineId`` field
(null when not yet ingested), so we join on that rather than chassis serial.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/expected-machine?siteId={site_id}
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
    "expected_count": 4,
    "ingested_count": 4,
    "matched_count": 4,
    "missing": [],
    "extra": [],
    "machines": [
      {
        "chassis_serial": "1871125000734",
        "expected_machine_id": "...",
        "machine_id": "...",
        "status": "Ready",
        "health": "healthy",
        "gpu_count": 4,
        "dpu_count": 2,
        "capabilities": ["GPU", "DPU", "InfiniBand"]
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> python verify_ingestion.py --org <org> --site-id <uuid> --api-base <url>

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

from common.nico_client import (
    NicoAuthError,
    classify_health,
    forge_get_all,
    resolve_auth,
    sum_capabilities,
)


def main() -> int:
    """Compare the expected-machine manifest with discovered machines and print JSON."""
    parser = argparse.ArgumentParser(description="Verify NICo hardware ingestion")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument(
        "--api-base",
        required=True,
        help="NICo API base URL",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "expected_count": 0,
        "ingested_count": 0,
        "matched_count": 0,
        "missing": [],
        "extra": [],
        "machines": [],
    }

    try:
        auth = resolve_auth()

        # Fetch all expected machines (paginated). Each carries machineId, the
        # authoritative link to a discovered machine (null when not ingested).
        expected_machines = forge_get_all(
            args.org,
            "expected-machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="expectedMachines",
        )

        # Fetch all actual machines (paginated). status, machineCapabilities and
        # health are top-level, so no metadata is needed -- we join by machine id.
        actual_machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="machines",
        )

        actual_by_id: dict[str, dict[str, Any]] = {m["id"]: m for m in actual_machines if m.get("id")}
        linked_machine_ids: set[str] = set()
        machines_detail: list[dict[str, Any]] = []

        for em in expected_machines:
            expected_machine_id = em.get("id", "")
            machine_id = em.get("machineId")
            # Manifest chassis serial, kept purely as a debug aid (matching is by id).
            chassis_serial = em.get("chassisSerialNumber", "")

            if not machine_id:
                result["missing"].append({"expected_machine_id": expected_machine_id, "chassis_serial": chassis_serial})
                machines_detail.append(
                    {
                        "chassis_serial": chassis_serial,
                        "expected_machine_id": expected_machine_id,
                        "machine_id": None,
                        "status": "NotFound",
                        "health": "unknown",
                        "gpu_count": 0,
                        "dpu_count": 0,
                        "capabilities": [],
                    }
                )
                continue

            linked_machine_ids.add(machine_id)
            actual = actual_by_id.get(machine_id) or {}
            capabilities = actual.get("machineCapabilities") or []
            cap_types = list({c.get("type", "") for c in capabilities})
            health = actual.get("health") or {}

            machines_detail.append(
                {
                    "chassis_serial": chassis_serial,
                    "expected_machine_id": expected_machine_id,
                    "machine_id": machine_id,
                    "status": actual.get("status", "Unknown"),
                    "health": classify_health(health),
                    "gpu_count": sum_capabilities(capabilities, "GPU"),
                    "dpu_count": sum_capabilities(capabilities, "DPU"),
                    "capabilities": cap_types,
                }
            )

        # Find extra machines (discovered but not linked to any expected machine)
        for machine_id in actual_by_id:
            if machine_id not in linked_machine_ids:
                result["extra"].append({"machine_id": machine_id})

        matched = [m for m in machines_detail if m.get("machine_id") is not None]

        result["expected_count"] = len(expected_machines)
        result["ingested_count"] = len(actual_machines)
        result["matched_count"] = len(matched)
        result["machines"] = machines_detail
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
