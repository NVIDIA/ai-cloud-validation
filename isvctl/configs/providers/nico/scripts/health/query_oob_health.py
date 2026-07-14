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

"""Query out-of-band (BMC) failure-detection coverage for NICo machines (STG04-01).

STG04 requires the platform to detect hardware failures out-of-band — device,
network, memory, and drive issues surfaced before or without relying on the
tenant OS. NICo aggregates BMC sensor data into the machine ``health`` report
(``BmcSensor``, ``BmcLeakDetection``, ...). This script maps those probes into
provider-neutral per-category observability records so
``OobFailureDetectionCheck`` can assert the OOB health API is present and
covers the required failure classes.

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
        "oob_health_present": true,
        "bmc_probe_ids": ["BmcSensor"],
        "failure_categories": {
          "device": {"observable": true, "probe_ids": ["BmcSensor"]},
          "network": {"observable": false, "probe_ids": []},
          "memory": {"observable": false, "probe_ids": []},
          "drive": {"observable": false, "probe_ids": []}
        }
      }
    ]
  }

A site with no ingested machines emits a structured skip (``skipped`` /
``skip_reason``) so the validation does not hard-fail a site with no hardware
discovered yet.

Usage:
    NICO_BEARER_TOKEN=<token> python query_oob_health.py \
        --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    Probe IDs:        infra-controller docs/architecture/health/health_probe_ids.md
    OpenAPI spec:     rest-api/openapi/spec.yaml (MachineHealth / MachineHealthProbe*)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, probe_text, resolve_auth

# BMC / out-of-band probe identifiers NICo reports on the machine health API.
BMC_PROBE_PREFIX = "bmc"

# Failure classes STG04-01 calls out, mapped by probe id/target/message keywords.
FAILURE_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "device": ("power", "fan", "temp", "thermal", "voltage", "gpu", "cpu", "sensor", "psu"),
    "network": ("nic", "link", "ethernet", "infiniband", "network", "bgp", "mlx"),
    "memory": ("memory", "ecc", "dimm", "rowremap", "row_remap", "hbm", "sram"),
    "drive": ("disk", "nvme", "hdd", "storage", "drive", "boss", "raid"),
}


def _is_bmc_probe(probe: dict[str, Any]) -> bool:
    """Return whether a probe is BMC/out-of-band sourced."""
    probe_id = str(probe.get("id") or "").lower()
    return probe_id.startswith(BMC_PROBE_PREFIX)


def _category_observability(probes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map NICo health probes into STG04 failure-category observability."""
    probe_texts = [(probe, probe_text(probe)) for probe in probes]

    categories: dict[str, dict[str, Any]] = {}
    for category, keywords in FAILURE_CATEGORY_KEYWORDS.items():
        matched_ids = sorted(
            {
                str(probe.get("id") or "")
                for probe, text in probe_texts
                if probe.get("id") and any(keyword in text for keyword in keywords)
            }
        )
        categories[category] = {
            "observable": bool(matched_ids),
            "probe_ids": matched_ids,
        }
    return categories


def host_record(machine: dict[str, Any]) -> dict[str, Any]:
    """Build the provider-neutral OOB failure-detection record for one machine."""
    health = machine.get("health") or {}
    probes = [p for p in (*(health.get("successes") or []), *(health.get("alerts") or [])) if isinstance(p, dict)]

    bmc_probes = [p for p in probes if _is_bmc_probe(p)]
    bmc_probe_ids = sorted({str(p.get("id") or "") for p in bmc_probes if p.get("id")})
    oob_present = bool(bmc_probe_ids or health.get("observedAt"))

    return {
        "host_id": machine.get("id", ""),
        "oob_health_present": oob_present,
        "bmc_probe_ids": bmc_probe_ids,
        "failure_categories": _category_observability(bmc_probes),
    }


def main() -> int:
    """Query NICo machine health and print per-host OOB failure-detection JSON."""
    parser = argparse.ArgumentParser(description="Query NICo out-of-band failure-detection coverage")
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

        if not machines:
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No machines found at site; no hosts to report OOB failure detection for"
            print(json.dumps(result, indent=2))
            return 0

        result["hosts"] = [host_record(machine) for machine in machines]
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
