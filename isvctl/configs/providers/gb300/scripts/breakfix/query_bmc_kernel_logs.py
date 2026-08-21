#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Obtain GB300 BMC log messages with read-only Redfish requests (BFX03-03).

The script is intended to run on a GB300 BCM head. It resolves one configured
compute node's BMC endpoint and credentials from read-only ``cmsh`` inventory,
then discovers a Manager-scoped Redfish BMC Journal LogService. Credentials and
raw log messages remain inside the privileged subprocess and are never emitted.
TLS uses the BCM host's system trust store or an explicitly configured BMC CA.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODULE_SETUP = """\
export MODULEPATH=/cm/local/modulefiles:/cm/shared/modulefiles
source /cm/local/apps/environment-modules/current/init/bash
module load shared cmsh >/dev/null 2>&1
"""
_QUERY_SCRIPT = (
    _MODULE_SETUP
    + """\
node_host="$1"
bmc_ca_cert="$2"

tls_args=()
if [ -n "$bmc_ca_cert" ]; then
    if [ ! -f "$bmc_ca_cert" ] || [ ! -r "$bmc_ca_cert" ]; then
        exit 24
    fi
    tls_args+=(--cacert "$bmc_ca_cert")
fi

bmc_ip=$(cmsh-lazy-load -c "device; use $node_host; interfaces; get rf0 ip" 2>/dev/null || true)
if [ -z "$bmc_ip" ]; then
    bmc_ip=$(cmsh-lazy-load -c "device; use $node_host; interfaces; get ipmi0 ip" 2>/dev/null || true)
fi

bmc_creds=$(cmsh-lazy-load -c "device; use $node_host; bmcsettings; get username; get password" 2>/dev/null || true)
bmc_user=$(printf '%s\n' "$bmc_creds" | sed -n '1p')
bmc_pass=$(printf '%s\n' "$bmc_creds" | sed -n '2p')
if [ -z "$bmc_user" ] || [ -z "$bmc_pass" ]; then
    category=$(cmsh-lazy-load -c "device; use $node_host; get category" 2>/dev/null || true)
    bmc_user=$(cmsh-lazy-load -c "category; use $category; bmcsettings; get username" 2>/dev/null || true)
    bmc_pass=$(cmsh-lazy-load -c "category; use $category; bmcsettings; get password" 2>/dev/null || true)
fi

if [ -z "$bmc_ip" ] || [ -z "$bmc_user" ] || [ -z "$bmc_pass" ]; then
    exit 20
fi

netrc_file=$(mktemp)
response_file=$(mktemp)
trap 'rm -f "$netrc_file" "$response_file"' EXIT
chmod 600 "$netrc_file" "$response_file"
printf 'default login %s password %s\n' "$bmc_user" "$bmc_pass" > "$netrc_file"

get_redfish() {
    curl --max-time 30 --connect-timeout 10 --fail --silent --show-error \
        "${tls_args[@]}" --netrc-file "$netrc_file" "https://$bmc_ip$1"
}

managers=$(get_redfish "/redfish/v1/Managers") || exit 21
while IFS= read -r manager_uri; do
    [ -n "$manager_uri" ] || continue
    manager=$(get_redfish "$manager_uri") || continue
    services_uri=$(printf '%s' "$manager" | jq -r '.LogServices."@odata.id" // empty')
    [ -n "$services_uri" ] || services_uri="$manager_uri/LogServices"
    services=$(get_redfish "$services_uri") || continue

    while IFS= read -r service_uri; do
        [ -n "$service_uri" ] || continue
        service=$(get_redfish "$service_uri") || continue
        service_id=$(printf '%s' "$service" | jq -r '.Id // empty')
        service_identity=$(
            printf '%s' "$service" | jq -r '[(.Name // ""), (.Description // "")] | join(" ") | ascii_downcase'
        )
        if [ "$service_id" != "Journal" ] || [[ "$service_identity" != *bmc*journal* ]]; then
            continue
        fi

        entries_uri=$(printf '%s' "$service" | jq -r '.Entries."@odata.id" // empty')
        [ -n "$entries_uri" ] || continue
        get_redfish "$entries_uri" > "$response_file" || continue
        message_count=$(
            jq '[.Members[]? | select(type == "object" and ((.Message // "") | strings | length > 0))] | length' \
                "$response_file"
        )
        if [ "$message_count" -lt 1 ]; then
            continue
        fi
        jq -n --arg log_source "$entries_uri" --argjson message_count "$message_count" \
            '{log_source: $log_source, message_count: $message_count}'
        exit 0
    done < <(printf '%s' "$services" | jq -r '.Members[]?."@odata.id" // empty')
done < <(printf '%s' "$managers" | jq -r '.Members[]?."@odata.id" // empty')

exit 23
"""
)


class InspectionError(RuntimeError):
    """Raised when direct BMC log evidence cannot be obtained."""


def _emit(result: dict[str, Any]) -> int:
    """Print provider-neutral JSON and return its exit status."""
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def _validate_host(host: str) -> str:
    """Reject values that cannot be safely used as BCM device names."""
    value = host.strip()
    if not _HOST_RE.fullmatch(value):
        raise InspectionError("invalid GB300 node hostname")
    return value


def _run_privileged(host: str, ca_cert: str = "") -> subprocess.CompletedProcess[str]:
    """Run the fixed read-only BCM and Redfish helper as root."""
    return subprocess.run(
        ["sudo", "-n", "bash", "-s", "--", _validate_host(host), ca_cert],
        input=_QUERY_SCRIPT,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )


def _query_host(host: str, ca_cert: str = "") -> dict[str, Any]:
    """Return normalized evidence for one GB300 compute-node BMC."""
    node_host = _validate_host(host)
    completed = _run_privileged(node_host, ca_cert)
    if completed.returncode != 0:
        raise InspectionError("unable to retrieve a non-empty GB300 BMC Journal log")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InspectionError("GB300 BMC returned invalid Journal evidence JSON") from exc

    log_source = payload.get("log_source") if isinstance(payload, dict) else None
    source_pattern = r"^/redfish/v1/Managers/[^/]+/LogServices/Journal/Entries/?$"
    if not isinstance(log_source, str) or not re.fullmatch(source_pattern, log_source):
        raise InspectionError("GB300 BMC Journal evidence has an invalid log source")
    message_count = payload.get("message_count")
    if isinstance(message_count, bool) or not isinstance(message_count, int) or message_count < 1:
        raise InspectionError("GB300 BMC Journal returned no log messages")

    return {
        "host_id": node_host,
        "kernel_log_available": True,
        "message_count": message_count,
        "log_source": log_source,
    }


def main() -> int:
    """Retrieve BMC log messages for the configured GB300 node."""
    parser = argparse.ArgumentParser(description="Obtain GB300 BMC log messages through Redfish")
    parser.add_argument(
        "--node-host",
        default=os.environ.get("GB300_NODE_HOST", ""),
        help="GB300 compute-node hostname (default: GB300_NODE_HOST)",
    )
    parser.add_argument(
        "--ca-cert",
        default=os.environ.get("GB300_BMC_CA_CERT", ""),
        help="Trusted BMC CA certificate (default: system trust store or GB300_BMC_CA_CERT)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "gb300",
        "source": "redfish Manager BMC Journal GET",
        "hosts": [],
    }
    try:
        if not args.node_host.strip():
            raise InspectionError("GB300_NODE_HOST is required")
        result["hosts"] = [_query_host(args.node_host, args.ca_cert)]
    except (InspectionError, subprocess.TimeoutExpired) as exc:
        result["error_type"] = "bmc_log_inspection"
        result["error"] = str(exc) if isinstance(exc, InspectionError) else "GB300 BMC log inspection timed out"
        return _emit(result)

    result["success"] = True
    return _emit(result)


if __name__ == "__main__":
    sys.exit(main())
