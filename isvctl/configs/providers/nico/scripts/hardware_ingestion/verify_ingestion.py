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
actually discovered machines. Matches by chassis serial number.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/expected-machine?siteId={site_id}
  GET /v2/org/{org}/carbide/machine?siteId={site_id}&includeMetadata=true

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_ISSUER_URL,
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
    NICO_BEARER_TOKEN=<token> python verify_ingestion.py --org <org> --site-id <uuid>

Reference:
    OpenAPI spec: ncp-isv-carbide-proxy-service/src/main/resources/docs/openapi/forge_api.yaml
"""

import argparse
import json
import sys
from pathlib import Path

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import (
    DEFAULT_API_BASE,
    NicoAuthError,
    classify_health,
    forge_get_all,
    resolve_auth,
    sum_capabilities,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify NICo hardware ingestion")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help="NICo API base URL (default: NGC production)",
    )
    args = parser.parse_args()

    result: dict = {
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

        # Fetch all expected machines (paginated)
        expected_machines = forge_get_all(
            args.org,
            "expected-machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="expectedMachines",
        )

        # Fetch all actual machines with metadata (paginated)
        actual_machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id, "includeMetadata": "true"},
            result_key="machines",
        )

        # Build lookup by chassis serial, tracking collisions
        actual_by_serial: dict[str, dict] = {}
        for m in actual_machines:
            metadata = m.get("metadata", {})
            dmi = metadata.get("dmiData", {})
            serial = dmi.get("chassisSerial", m.get("id", ""))
            if serial in actual_by_serial:
                # Collision: multiple machines with same serial -- keep both IDs
                existing_id = actual_by_serial[serial].get("id", "?")
                new_id = m.get("id", "?")
                result.setdefault("warnings", []).append(
                    f"Duplicate chassis serial {serial}: machines {existing_id} and {new_id}"
                )
            actual_by_serial[serial] = m

        expected_serials: set[str] = set()
        machines_detail: list[dict] = []

        for em in expected_machines:
            serial = em.get("chassisSerialNumber", "")
            expected_serials.add(serial)

            actual = actual_by_serial.get(serial)
            if actual:
                capabilities = actual.get("machineCapabilities", [])
                cap_types = list({c.get("type", "") for c in capabilities})
                health = actual.get("health", {})

                machines_detail.append(
                    {
                        "chassis_serial": serial,
                        "expected_machine_id": em.get("id", ""),
                        "machine_id": actual.get("id", ""),
                        "status": actual.get("status", "Unknown"),
                        "health": classify_health(health),
                        "gpu_count": sum_capabilities(capabilities, "GPU"),
                        "dpu_count": sum_capabilities(capabilities, "DPU"),
                        "capabilities": cap_types,
                    }
                )
            else:
                result["missing"].append(
                    {
                        "chassis_serial": serial,
                        "expected_machine_id": em.get("id", ""),
                    }
                )
                machines_detail.append(
                    {
                        "chassis_serial": serial,
                        "expected_machine_id": em.get("id", ""),
                        "machine_id": None,
                        "status": "NotFound",
                        "health": "unknown",
                        "gpu_count": 0,
                        "dpu_count": 0,
                        "capabilities": [],
                    }
                )

        # Find extra machines (ingested but not expected)
        actual_serials = set(actual_by_serial.keys())
        extra_serials = actual_serials - expected_serials
        for serial in sorted(extra_serials):
            m = actual_by_serial[serial]
            result["extra"].append(
                {
                    "chassis_serial": serial,
                    "machine_id": m.get("id", ""),
                }
            )

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
