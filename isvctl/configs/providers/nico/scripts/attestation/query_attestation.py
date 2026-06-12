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

"""Query NICo SPDM attestation status for bare-metal machines.

NICo exposes nonce-based device attestation on the operator/control-plane Forge
gRPC API, not on the tenant REST API. This script bridges the two surfaces:

1. Use the tenant REST API to enumerate site machines.
2. Use ``nico-admin-cli attestation spdm list`` to read control-plane SPDM
   attestation statuses.
3. Use ``nico-admin-cli attestation measured-boot machine show`` to read
   measured-boot machine states.
4. Emit the provider-neutral ``query_attestation`` contract consumed by
   ``NonceAttestationCheck`` and ``FirmwareAttestationCheck``.

The admin CLI must be configured with a Forge control-plane URL and an authorized
client certificate. The script accepts explicit CLI TLS arguments, but also works
when the admin CLI is configured via its standard environment variables.

Measured boot state ``Measured`` means NICo matched the machine's boot
measurements against an active golden bundle. NICo enables secure boot during
host bring-up for these measured hosts, so this adapter treats ``Measured`` as
both signed-firmware enforcement and successful boot-measurement attestation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, resolve_auth

SPDM_PASSED = "SPDM_ATT_PASSED"
MEASURED_BOOT_PASSED = "Measured"
SUPPORTED_SPDM_STATUSES = {
    "SPDM_ATT_IN_PROGRESS",
    "SPDM_ATT_CANCELLED",
    SPDM_PASSED,
    "SPDM_ATT_FAILED",
}
SUPPORTED_MEASURED_BOOT_STATES = {
    "Discovered",
    "PendingBundle",
    MEASURED_BOOT_PASSED,
    "MeasuringFailed",
}


def parse_json_output(text: str) -> Any:
    """Parse JSON from admin-cli output, tolerating warning lines before it."""
    decoder = json.JSONDecoder()
    for offset, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            payload, end = decoder.raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        if text[offset + end :].strip():
            continue
        return payload
    raise ValueError("admin CLI output did not contain JSON")


def build_admin_cli_command(args: argparse.Namespace) -> list[str]:
    """Build the common nico-admin-cli prefix used for control-plane attestation queries."""
    command = [args.admin_cli, "--format", "json"]
    optional_args = [
        ("--carbide-url", args.carbide_url),
        ("--forge-root-ca-path", args.forge_root_ca_path),
        ("--client-key-path", args.client_key_path),
        ("--client-cert-path", args.client_cert_path),
    ]
    for flag, value in optional_args:
        if value:
            command.extend([flag, value])
    return command


def build_spdm_command(args: argparse.Namespace) -> list[str]:
    """Build the admin CLI command used to query SPDM attestation status."""
    return [*build_admin_cli_command(args), "attestation", "spdm", "list"]


def build_measured_boot_command(args: argparse.Namespace) -> list[str]:
    """Build the admin CLI command used to query measured-boot machine states."""
    return [*build_admin_cli_command(args), "attestation", "measured-boot", "machine", "show"]


def run_admin_cli_json(command: list[str], *, timeout: int) -> Any:
    """Run an admin CLI command and parse JSON from stdout."""
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"admin CLI failed with exit code {completed.returncode}: {detail}")
    return parse_json_output(completed.stdout)


def admin_cli_available(command: str) -> bool:
    """Return whether the admin CLI command can be executed."""
    if os.path.sep in command:
        path = Path(command)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


def spdm_status_map(payload: Any) -> dict[str, str]:
    """Convert ``nico-admin-cli attestation spdm list`` JSON into machine -> status."""
    if not isinstance(payload, list):
        raise ValueError("SPDM list output must be a JSON list")

    statuses: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise ValueError(f"SPDM list item must be [machine_id, status], got: {item!r}")
        machine_id, status = item
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError(f"SPDM list item has invalid machine_id: {item!r}")
        if not isinstance(status, str) or status not in SUPPORTED_SPDM_STATUSES:
            raise ValueError(f"SPDM list item has unsupported status: {item!r}")
        statuses[machine_id] = status
    return statuses


def measured_boot_state_map(payload: Any) -> dict[str, str]:
    """Convert measured-boot machine JSON into machine -> measured boot state."""
    if not isinstance(payload, list):
        raise ValueError("measured-boot machine output must be a JSON list")

    states: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"measured-boot machine item must be an object, got: {item!r}")
        machine_id = item.get("machine_id")
        state = item.get("state")
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError(f"measured-boot machine item has invalid machine_id: {item!r}")
        if not isinstance(state, str) or state not in SUPPORTED_MEASURED_BOOT_STATES:
            raise ValueError(f"measured-boot machine item has unsupported state: {item!r}")
        states[machine_id] = state
    return states


def machine_attestation_record(
    machine: dict[str, Any],
    spdm_statuses: dict[str, str],
    measured_boot_states: dict[str, str],
) -> dict[str, Any]:
    """Build one provider-neutral attestation record for a NICo machine."""
    machine_id = machine.get("id", "")
    spdm_status = spdm_statuses.get(machine_id)
    measured_boot_state = measured_boot_states.get(machine_id)
    spdm_passed = spdm_status == SPDM_PASSED
    measured_boot_passed = measured_boot_state == MEASURED_BOOT_PASSED

    return {
        "machine_id": machine_id,
        "status": machine.get("status", "Unknown"),
        "attestation_supported": spdm_status is not None or measured_boot_state is not None,
        "nonce_verified": spdm_passed,
        "attestation_signature_valid": spdm_passed,
        "spdm_attestation_status": spdm_status or "not_found",
        "secure_boot_enabled": measured_boot_passed,
        "boot_measurements_attested": measured_boot_passed,
        "measured_boot_state": measured_boot_state or "not_found",
    }


def main() -> int:
    """Query tenant machines + SPDM attestation status and print JSON."""
    parser = argparse.ArgumentParser(description="Query NICo SPDM attestation status")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo tenant REST API base URL")
    parser.add_argument(
        "--admin-cli",
        default=os.environ.get("NICO_ADMIN_CLI", "nico-admin-cli"),
        help="Path to nico-admin-cli/carbide-admin-cli (default: NICO_ADMIN_CLI or nico-admin-cli)",
    )
    parser.add_argument("--carbide-url", default=os.environ.get("NICO_CARBIDE_URL", ""), help="Forge gRPC URL")
    parser.add_argument(
        "--forge-root-ca-path",
        default=os.environ.get("NICO_FORGE_ROOT_CA_PATH", ""),
        help="Forge root CA path for the admin CLI",
    )
    parser.add_argument(
        "--client-key-path",
        default=os.environ.get("NICO_CLIENT_KEY_PATH", ""),
        help="Admin client key path for the admin CLI",
    )
    parser.add_argument(
        "--client-cert-path",
        default=os.environ.get("NICO_CLIENT_CERT_PATH", ""),
        help="Admin client certificate path for the admin CLI",
    )
    parser.add_argument("--admin-timeout", type=int, default=30, help="Admin CLI timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "skipped": False,
        "machines_checked": 0,
        "machines": [],
    }

    try:
        if not admin_cli_available(args.admin_cli):
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = (
                f"NICo admin CLI '{args.admin_cli}' is not available; set NICO_ADMIN_CLI "
                "or install nico-admin-cli to run attestation checks"
            )
            print(json.dumps(result, indent=2))
            return 0

        auth = resolve_auth()
        machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="machines",
        )
        spdm_statuses = spdm_status_map(run_admin_cli_json(build_spdm_command(args), timeout=args.admin_timeout))
        measured_boot_states = measured_boot_state_map(
            run_admin_cli_json(build_measured_boot_command(args), timeout=args.admin_timeout)
        )

        result["machines"] = [
            machine_attestation_record(machine, spdm_statuses, measured_boot_states) for machine in machines
        ]
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
