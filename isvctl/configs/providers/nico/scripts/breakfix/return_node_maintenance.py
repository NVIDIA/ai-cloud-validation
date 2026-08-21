#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Return one explicit NICo machine for maintenance and restore it (BFX01-02).

The caller must name a dedicated fixture. The script never discovers or selects
a mutation target on its own. It verifies NICo reports the Machine in
``Maintenance`` after the request, then disables maintenance mode in ``finally``
so failed assertions do not strand the fixture.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from breakfix._common import emit
from common.nico_client import NicoAuthError, forge_get, forge_patch, resolve_auth

MUTATION_TIMEOUT_SECONDS = 300
RESTORE_TIMEOUT_SECONDS = 120
RESTORE_POLL_INTERVAL_SECONDS = 2.0
MAINTENANCE_MESSAGE = "ISV BFX01-02 validation fixture; automatically restored"


def _operation(machine_id: str) -> dict[str, Any]:
    """Build the provider-neutral operation result with safe defaults."""
    return {
        "requested": False,
        "accepted": False,
        "machine_id": machine_id,
        "maintenance_mode": "",
        "restored": False,
    }


def _api_error(exc: Exception) -> str:
    """Return a concise API error without response payloads or credentials."""
    return f"{type(exc).__name__}: {exc}"


def _machine_status(machine: dict[str, Any]) -> str:
    """Return a normalized Machine status string."""
    return str(machine.get("status") or "").strip()


def _has_binding(machine: dict[str, Any], key: str) -> bool:
    """Return whether a Machine has a non-empty allocation identifier."""
    value = machine.get(key)
    return value is not None and bool(str(value).strip())


def _wait_for_status(
    org: str,
    machine_path: str,
    token: str,
    *,
    base_url: str,
    expected_status: str,
) -> dict[str, Any]:
    """Poll NICo until the Machine returns to its exact initial status."""
    deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
    while True:
        current = forge_get(org, machine_path, token, base_url=base_url)
        if _machine_status(current) == expected_status or time.monotonic() >= deadline:
            return current
        time.sleep(RESTORE_POLL_INTERVAL_SECONDS)


def main() -> int:
    """Request and verify maintenance mode, then restore the explicit fixture."""
    parser = argparse.ArgumentParser(description="Return an explicit NICo machine for maintenance")
    parser.add_argument("--org", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--machine-id", default="", help="Dedicated staging Machine ID; no automatic selection")
    parser.add_argument("--allow-mutation", default="", help="Must be exactly 1 to mutate the staging fixture")
    args = parser.parse_args()

    machine_id = args.machine_id.strip()
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "operation": _operation(machine_id),
    }
    operation = result["operation"]

    if not machine_id:
        result.update(
            {
                "success": True,
                "skipped": True,
                "skip_reason": (
                    "No dedicated maintenance fixture configured; set NICO_BREAKFIX_MACHINE_ID "
                    "to a staging Machine that may be mutated and restored"
                ),
            }
        )
        return emit(result)
    if args.allow_mutation != "1":
        result.update(
            {
                "success": True,
                "skipped": True,
                "skip_reason": (
                    "NICo maintenance mutation is disabled; set NICO_BREAKFIX_ALLOW_MUTATION=1 "
                    "only for an approved staging fixture"
                ),
            }
        )
        return emit(result)

    machine_path = f"machine/{quote(machine_id, safe='')}"
    try:
        auth = resolve_auth()
        initial = forge_get(args.org, machine_path, auth.token, base_url=args.api_base)
    except (NicoAuthError, URLError, ValueError) as exc:
        result["error"] = _api_error(exc)
        return emit(result)

    if initial.get("id") != machine_id:
        result["error"] = "NICo returned a different Machine than the configured maintenance fixture"
        return emit(result)
    if initial.get("siteId") != args.site_id:
        result["error"] = "Configured maintenance fixture does not belong to the configured Site"
        return emit(result)

    initial_status = _machine_status(initial)
    if not initial_status:
        result["error"] = "Configured maintenance fixture has no observable status"
        return emit(result)
    if initial_status == "Maintenance":
        result["error"] = "Configured maintenance fixture is already in Maintenance; refusing to take ownership"
        return emit(result)
    if initial_status != "Ready":
        result["error"] = "Configured maintenance fixture must be Ready before validation"
        return emit(result)
    if _has_binding(initial, "instanceId") or _has_binding(initial, "tenantId"):
        result["error"] = "Configured maintenance fixture is allocated; refusing to mutate it"
        return emit(result)

    maintenance_attempted = False
    cleanup_errors: list[str] = []
    try:
        operation["requested"] = True
        maintenance_attempted = True
        updated = forge_patch(
            args.org,
            machine_path,
            auth.token,
            base_url=args.api_base,
            body={"setMaintenanceMode": True, "maintenanceMessage": MAINTENANCE_MESSAGE},
            timeout=MUTATION_TIMEOUT_SECONDS,
        )
        current = forge_get(args.org, machine_path, auth.token, base_url=args.api_base)
        updated_status = _machine_status(updated)
        current_status = _machine_status(current)
        operation["maintenance_mode"] = current_status
        operation["accepted"] = updated_status == "Maintenance" and current_status == "Maintenance"
        if not operation["accepted"]:
            result["error"] = (
                "NICo did not confirm Maintenance state "
                f"(response={updated_status or 'missing'}, current={current_status or 'missing'})"
            )
    except (NicoAuthError, URLError, ValueError) as exc:
        result["error"] = _api_error(exc)
    finally:
        if maintenance_attempted:
            try:
                forge_patch(
                    args.org,
                    machine_path,
                    auth.token,
                    base_url=args.api_base,
                    body={"setMaintenanceMode": False},
                    timeout=MUTATION_TIMEOUT_SECONDS,
                )
                restored_current = _wait_for_status(
                    args.org,
                    machine_path,
                    auth.token,
                    base_url=args.api_base,
                    expected_status=initial_status,
                )
                current_status = _machine_status(restored_current)
                operation["restored"] = current_status == initial_status
                if not operation["restored"]:
                    cleanup_errors.append(
                        "NICo did not restore the fixture to its initial Ready state "
                        f"(current={current_status or 'missing'})"
                    )
            except (NicoAuthError, URLError, ValueError) as exc:
                cleanup_errors.append(_api_error(exc))

    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors
        result.setdefault("error", "Failed to restore the maintenance fixture")

    result["success"] = bool(operation["requested"] and operation["accepted"] and operation["restored"])
    if not result["success"]:
        result.setdefault("error", "Node maintenance validation did not complete")
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
