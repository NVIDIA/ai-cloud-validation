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

"""Audit tenant-transition data sanitization for a NICo site (SEC21-04/05/06).

When a tenant releases a host (the Instance is deleted), NICo runs its cleanup
and sanitization workflow before returning the host to the allocatable pool:
host/RAM cleanup and the UEFI MemoryOverwriteRequestControl check, NVMe/HDD
secure erase, InfiniBand cleanup, TPM clear, and BIOS/UEFI recommit. At the
REST level this whole workflow is the machine ``Reset`` status: a released host
moves ``InUse -> Reset -> ... -> Ready`` and must not return to ``Ready`` (nor
report ``isUsableByTenant``) until cleanup completes.

This script reads each machine's ``status`` and ``statusHistory`` and maps the
NICo lifecycle into a provider-neutral token sequence so the validations can
assert that no host went from ``in_use`` back to ``available`` without an
intervening ``sanitizing`` stage, and that no available host is still bound to
a prior tenant. GPU presence and firmware identity (vendor / product / BIOS)
are surfaced so the GPU-memory and firmware-reset checks can scope and report.

NICo MachineStatus enum (per the upstream OpenAPI spec):
  Initializing | Ready | Reset | Maintenance | InUse | Error | Decommissioned | Unknown

NICo -> neutral lifecycle token mapping:
  Reset -> sanitizing   (the cleanup/sanitization workflow between tenants)
  InUse -> in_use       (assigned to and running a tenant workload)
  Ready -> available    (allocatable to a new tenant)
  others -> lowercased status (maintenance, initializing, error, ...)

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
    "machines_checked": 2,
    "machines": [
      {
        "machine_id": "...",
        "status": "available",
        "available": true,
        "in_use": false,
        "has_gpu": true,
        "served_tenant": true,
        "sanitized": true,
        "stale_tenant_binding": false,
        "vendor": "Lenovo",
        "product_name": "ThinkSystem SR670 V2",
        "bios_version": "U8E122J-1.51",
        "transitions": ["in_use", "sanitizing", "available"]
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> python query_sanitization.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    Cleanup workflow: infra-controller docs/operations/tenant-lifecycle-cleanup.md
    State machine:    infra-controller docs/architecture/state_machines/managedhost.md
    OpenAPI spec:     rest-api/openapi/spec.yaml (Machine / MachineStatus / StatusDetail)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, resolve_auth

# Provider-neutral lifecycle tokens the validations gate on.
IN_USE = "in_use"
SANITIZING = "sanitizing"
AVAILABLE = "available"

# NICo MachineStatus -> neutral token. Statuses not listed are lowercased.
STATUS_TOKENS: dict[str, str] = {
    "InUse": IN_USE,
    "Reset": SANITIZING,
    "Ready": AVAILABLE,
}

# Cap the diagnostic transitions list so a long-lived host does not bloat output.
MAX_TRANSITIONS = 20


def status_token(status: str | None) -> str:
    """Map a NICo machine status to its provider-neutral lifecycle token."""
    if not status:
        return "unknown"
    return STATUS_TOKENS.get(status, status.lower())


def ordered_history_statuses(machine: dict[str, Any]) -> list[str]:
    """Return the machine's statuses in chronological order, including current.

    NICo records each status change in ``statusHistory`` ({status, message,
    created, updated}); entries are sorted by ``created`` (ISO 8601 sorts
    lexicographically). The live ``status`` is appended when it differs from
    the last recorded entry so the latest state is always represented.
    """
    history = machine.get("statusHistory") or []
    entries = [e for e in history if isinstance(e, dict) and e.get("status")]
    entries.sort(key=lambda e: e.get("created") or e.get("updated") or "")
    statuses = [str(e["status"]) for e in entries]

    current = machine.get("status")
    if current and (not statuses or statuses[-1] != current):
        statuses.append(str(current))
    return statuses


def evaluate_transitions(tokens: list[str]) -> tuple[bool, bool]:
    """Return ``(served_tenant, sanitized)`` from a neutral token sequence.

    ``served_tenant`` is true when the host ever ran a tenant workload
    (``in_use``). ``sanitized`` is false when the host returned to
    ``available`` after a tenancy without an intervening ``sanitizing`` stage.
    """
    served = IN_USE in tokens
    seen_in_use_since_sanitize = False
    sanitized = True
    for token in tokens:
        if token == IN_USE:
            seen_in_use_since_sanitize = True
        elif token == SANITIZING:
            seen_in_use_since_sanitize = False
        elif token == AVAILABLE and seen_in_use_since_sanitize:
            sanitized = False
    return served, sanitized


def has_gpu(machine: dict[str, Any]) -> bool:
    """Return whether the machine reports any GPU capability."""
    capabilities = machine.get("machineCapabilities") or []
    return any(isinstance(c, dict) and c.get("type") == "GPU" for c in capabilities)


def machine_record(machine: dict[str, Any]) -> dict[str, Any]:
    """Build the provider-neutral sanitization record for one NICo machine."""
    statuses = ordered_history_statuses(machine)
    tokens = [status_token(s) for s in statuses]
    served, sanitized = evaluate_transitions(tokens)

    current = machine.get("status")
    usable = bool(machine.get("isUsableByTenant"))
    assigned = bool(machine.get("instanceId")) or bool(machine.get("tenantId"))
    is_ready = current == "Ready"

    dmi = (machine.get("metadata") or {}).get("dmiData") or {}

    return {
        "machine_id": machine.get("id", ""),
        "status": status_token(current),
        "available": is_ready and usable and not assigned,
        "in_use": current == "InUse",
        "has_gpu": has_gpu(machine),
        "served_tenant": served,
        "sanitized": sanitized,
        # Offered to new tenants (Ready + usable) while still bound to a prior
        # tenant's instance -- a hard sanitization failure if it ever occurs.
        "stale_tenant_binding": is_ready and usable and assigned,
        "vendor": machine.get("vendor") or "",
        "product_name": machine.get("productName") or "",
        "bios_version": dmi.get("biosVersion") or "",
        "transitions": tokens[-MAX_TRANSITIONS:],
    }


def main() -> int:
    """Query NICo machines and print per-machine sanitization records as JSON."""
    parser = argparse.ArgumentParser(description="Audit NICo tenant-transition sanitization")
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

        result["machines"] = [machine_record(machine) for machine in machines]
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
