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

"""Query stable hardware serial numbers for a NICo site (BFX03-01).

Break/fix workflows need to identify the physical hardware installed in a host:
chassis, baseboard, network interfaces (NICs), CPU, and GPU. NICo discovers this
inventory during ingestion and exposes it on the Machine resource. This script
reads the per-machine hardware metadata and reduces it to a provider-neutral
per-component identifier record so ``HardwareSerialCheck`` can assert that a
stable identifier is queryable for every component class that is present.

Per BFX03-01 the identifiers may be obfuscated as long as they are stable, so
this script never depends on globally-unique serials. Where NICo exposes a true
serial (chassis / baseboard / GPU) it is used; NICs are identified by their
stable MAC address (Ethernet) or GUID (InfiniBand); CPUs have no per-socket
serial in DMI, so the stable CPU model descriptor is reported as the identifier.
The identifier *values* are hardware asset IDs (not secrets), so they are
emitted as-is.

NICo Machine hardware sources (``includeMetadata=true``):
  - chassis:   ``metadata.dmiData.chassisSerial``
  - baseboard: ``metadata.dmiData.boardSerial``
  - gpu:       ``metadata.gpus[].serial``
  - nic:       ``metadata.networkInterfaces[].macAddress`` +
               ``metadata.infinibandInterfaces[].guid``
  - cpu:       ``machineCapabilities[type=CPU]`` (model descriptor; no serial)
  - machine:   top-level ``serialNumber`` (provider-visible), used as a
               chassis-serial fallback.

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
    "machines_checked": 1,
    "machines": [
      {
        "machine_id": "...",
        "components": {
          "chassis":   {"present": true, "identifiers": ["J1050ACR"]},
          "baseboard": {"present": true, "identifiers": [".C1KS2CS002G."]},
          "cpu":       {"present": true, "identifiers": ["Intel(R) Xeon(R) Gold 6354 CPU @ 3.00GHz"]},
          "gpu":       {"present": true, "identifiers": ["1654422006434"]},
          "nic":       {"present": true, "identifiers": ["c8:4b:d6:7b:ac:a8", "1070fd0300bd43ac"]}
        }
      }
    ]
  }

A site with no ingested machines emits a structured skip (``skipped`` /
``skip_reason``) so the validation does not hard-fail a site that has no
hardware discovered yet.

Usage:
    NICO_BEARER_TOKEN=<token> python query_serial_numbers.py \
        --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    OpenAPI spec: rest-api/openapi/spec.yaml
      (MachineMetadata / MachineDMIData / MachineGPUInfo /
       MachineNetworkInterface / MachineInfiniBandInterface / MachineCapability)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, resolve_auth


def _dedupe(identifiers: list[str]) -> list[str]:
    """Drop non-string/blank values and duplicates while preserving first-seen order."""
    seen: dict[str, None] = {}
    for identifier in identifiers:
        cleaned = identifier.strip() if isinstance(identifier, str) else ""
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen)


def _component(present: bool, identifiers: list[str]) -> dict[str, Any]:
    """Build a per-component record with presence and deduplicated identifiers."""
    return {"present": present, "identifiers": _dedupe(identifiers)}


def machine_serials(machine: dict[str, Any]) -> dict[str, Any]:
    """Build the provider-neutral per-component serial record for one machine.

    A component is ``present`` when the machine reports that hardware class at
    all (so a CPU-only storage node correctly reports ``gpu.present = false``
    rather than failing for a missing GPU serial). ``identifiers`` holds the
    stable IDs that were queryable for a present component; a present component
    with no queryable identifier yields an empty list, which the validation
    treats as a failure.
    """
    metadata = machine.get("metadata") or {}
    dmi = metadata.get("dmiData") or {}
    gpus = metadata.get("gpus") or []
    nics = metadata.get("networkInterfaces") or []
    ib_nics = metadata.get("infinibandInterfaces") or []
    capabilities = [c for c in (machine.get("machineCapabilities") or []) if isinstance(c, dict)]

    # Chassis: both the DMI chassis serial and the provider-visible machine
    # serial number are stable chassis identifiers; reporting both keeps the
    # component queryable when DMI leaves chassisSerial blank.
    chassis_ids = [dmi.get("chassisSerial"), machine.get("serialNumber")]

    cpu_ids = [c.get("name") for c in capabilities if c.get("type") == "CPU"]

    nic_ids = [n.get("macAddress") for n in nics if isinstance(n, dict)]
    nic_ids += [n.get("guid") for n in ib_nics if isinstance(n, dict)]

    gpu_ids = [g.get("serial") for g in gpus if isinstance(g, dict)]
    # A GPU host is one that reports GPUs in metadata or an ingested GPU
    # capability. A CPU/storage node legitimately has no GPU, so gpu.present
    # stays false there and the check does not expect a GPU serial.
    gpu_present = bool(gpus) or any(c.get("type") == "GPU" for c in capabilities)

    # Chassis, baseboard, CPU, and NIC exist on every physical host, so they are
    # always present: the check then requires each to expose a stable identifier.
    # GPU presence is conditional on the host actually having accelerators.
    return {
        "machine_id": machine.get("id", ""),
        "components": {
            "chassis": _component(present=True, identifiers=chassis_ids),
            "baseboard": _component(present=True, identifiers=[dmi.get("boardSerial")]),
            "cpu": _component(present=True, identifiers=cpu_ids),
            "gpu": _component(present=gpu_present, identifiers=gpu_ids),
            "nic": _component(present=True, identifiers=nic_ids),
        },
    }


def main() -> int:
    """Query NICo machines and print per-machine hardware serial records as JSON."""
    parser = argparse.ArgumentParser(description="Query NICo hardware serial numbers")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
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
            result["skip_reason"] = "No machines found at site; no hardware discovered to query serial numbers for"
            print(json.dumps(result, indent=2))
            return 0

        result["machines"] = [machine_serials(machine) for machine in machines]
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
