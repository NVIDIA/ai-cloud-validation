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

"""Query stable admin IP assignments for NICo machines (STG03-01).

Storage nodes require static IP addressing that remains stable across host
lifecycle operations and maintenance events. NICo assigns admin IPs through its
IPAM service and exposes them on each machine's ``machineInterfaces`` array.
This script reads the per-interface ``ipAddresses`` (preferring the primary
admin interface) and emits a provider-neutral record so
``StableStorageNodeIpCheck`` can assert every host reports at least one stable
admin IP.

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
        "hw_sku_device_type": "storage",
        "primary_ip_addresses": ["192.156.7.23"]
      }
    ]
  }

A site with no ingested machines emits a structured skip (``skipped`` /
``skip_reason``) so the validation does not hard-fail a site with no hardware
discovered yet.

Usage:
    NICO_BEARER_TOKEN=<token> python query_stable_ips.py \
        --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    OpenAPI spec: rest-api/openapi/spec.yaml (Machine.machineInterfaces /
      MachineInterface.ipAddresses / MachineInterface.isPrimary)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, resolve_auth


def _dedupe_ips(addresses: list[str]) -> list[str]:
    """Drop blank values and duplicates while preserving first-seen order."""
    seen: dict[str, None] = {}
    for address in addresses:
        cleaned = address.strip() if isinstance(address, str) else ""
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen)


def host_record(machine: dict[str, Any]) -> dict[str, Any]:
    """Build the provider-neutral stable-IP record for one NICo machine."""
    interfaces = [iface for iface in (machine.get("machineInterfaces") or []) if isinstance(iface, dict)]
    primary = [iface for iface in interfaces if iface.get("isPrimary")]
    targets = primary or interfaces

    primary_ips = _dedupe_ips([ip for iface in targets for ip in (iface.get("ipAddresses") or [])])

    return {
        "host_id": machine.get("id", ""),
        "hw_sku_device_type": machine.get("hwSkuDeviceType") or "",
        "primary_ip_addresses": primary_ips,
    }


def main() -> int:
    """Query NICo machines and print per-host stable admin IP JSON."""
    parser = argparse.ArgumentParser(description="Query NICo stable machine admin IPs")
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
            result["skip_reason"] = "No machines found at site; no hosts to report stable admin IPs for"
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
