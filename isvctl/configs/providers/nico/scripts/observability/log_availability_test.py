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

"""NICo observability log availability probes for fabric and switch evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.ufm_client import (
    UfmAuthError,
    describe_http_error,
    describe_url_error,
    get_event_history,
    get_log_text,
    resolve_ufm_auth,
    ufm_configured,
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

ASPECT_TESTS: dict[str, list[str]] = {
    "ufm_event_logs": [
        "event_log_endpoint_reachable",
        "event_log_source_present",
        "event_entries_queryable",
    ],
    "fabric_manager_logs": [
        "log_endpoint_reachable",
        "log_source_present",
        "log_entries_queryable",
    ],
    "subnet_manager_logs": [
        "log_endpoint_reachable",
        "log_source_present",
        "log_entries_queryable",
    ],
    "general_switch_logs": [
        "log_endpoint_reachable",
        "switch_log_source_present",
        "entries_queryable",
    ],
    "switch_syslogs": [
        "syslog_endpoint_reachable",
        "switch_syslog_source_present",
        "entries_recent",
    ],
    "switch_kernel_logs": [
        "log_endpoint_reachable",
        "kernel_log_source_present",
        "entries_queryable",
    ],
}

DEMO_PROBES: dict[str, dict[str, Any]] = {
    "ufm_event_logs": {
        "log_endpoints_checked": 1,
        "log_source": "demo-ufm-event-log",
        "entry_count": 5,
        "latest_timestamp": "2026-05-20T13:19:00Z",
    },
    "fabric_manager_logs": {
        "log_endpoints_checked": 1,
        "log_source": "demo-fabric-manager-log",
        "entry_count": 7,
        "latest_timestamp": "2026-05-20T13:18:30Z",
    },
    "subnet_manager_logs": {
        "log_endpoints_checked": 1,
        "log_source": "demo-subnet-manager-log",
        "entry_count": 6,
        "latest_timestamp": "2026-05-20T13:18:00Z",
    },
    "general_switch_logs": {
        "switches_checked": 2,
        "log_source": "demo-switch-operational-log",
        "entry_count": 8,
        "latest_timestamp": "2026-05-20T13:18:00Z",
    },
    "switch_syslogs": {
        "switches_checked": 2,
        "log_source": "demo-switch-syslog",
        "entry_count": 10,
        "latest_timestamp": "2026-05-20T13:17:00Z",
    },
    "switch_kernel_logs": {
        "switches_checked": 2,
        "log_source": "demo-switch-kernel-log",
        "entry_count": 3,
        "latest_timestamp": "2026-05-20T13:16:00Z",
    },
}

NICO_SWITCH_LOGS_HIDDEN_MESSAGE = (
    "NICo tenant APIs do not expose customer-queryable switch syslog or kernel logs; "
    "switch log collection is provider-operated"
)


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
    }


def _passed(message: str, probes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a passing subtest result."""
    result: dict[str, Any] = {"passed": True, "message": message}
    if probes is not None:
        result["probes"] = probes
    return result


def _failed(error: str, probes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a failing subtest result."""
    result: dict[str, Any] = {"passed": False, "error": error}
    if probes is not None:
        result["probes"] = probes
    return result


def _provider_hidden(test_name: str, *, probe_field: str, message: str) -> dict[str, Any]:
    """Build a passing provider-hidden subtest result."""
    return {
        "passed": True,
        "provider_hidden": True,
        "probes": {probe_field: 0},
        "message": f"{test_name}: {message}",
    }


def _event_timestamp(entry: dict[str, Any]) -> str:
    """Return the best available timestamp field from a UFM event entry."""
    for field in ("timestamp", "time", "event_time", "created_at", "date"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def check_ufm_event_logs(*, page_size: int = 10) -> dict[str, Any]:
    """Query UFM event history and emit the observability contract."""
    result = _base_result("ufm_event_logs")
    probes = {
        "log_endpoints_checked": 0,
        "log_source": "",
        "entry_count": 0,
        "latest_timestamp": "",
    }

    if not ufm_configured():
        error = "UFM access is not configured; set UFM_ADDRESS and UFM_TOKEN or UFM_USERNAME/UFM_PASSWORD"
        for name in ASPECT_TESTS["ufm_event_logs"]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    try:
        auth = resolve_ufm_auth()
        entries = get_event_history(auth, page_number=1, rpp=page_size)
    except HTTPError as e:
        error = describe_http_error(e)
        for name in ASPECT_TESTS["ufm_event_logs"]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result
    except (URLError, UfmAuthError) as e:
        error = describe_url_error(e) if isinstance(e, URLError) else str(e)
        for name in ASPECT_TESTS["ufm_event_logs"]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    probes = {
        "log_endpoints_checked": 1,
        "log_source": "ufm-event-history",
        "entry_count": len(entries),
        "latest_timestamp": _event_timestamp(entries[0]) if entries else "",
    }
    result["tests"]["event_log_endpoint_reachable"] = _passed("UFM event log endpoint reachable", probes)
    result["tests"]["event_log_source_present"] = _passed("UFM event log source present", probes)
    result["tests"]["event_entries_queryable"] = _passed(f"{len(entries)} UFM event log entries returned", probes)
    result["success"] = True
    return result


def _parse_log_lines(content: str) -> tuple[int, str]:
    """Count non-empty log lines and return the latest timestamp-like prefix."""
    lines = [line for line in content.splitlines() if line.strip()]
    latest_timestamp = ""
    if lines:
        candidate = lines[0].split()[0] if lines[0].split() else ""
        latest_timestamp = candidate
    return len(lines), latest_timestamp


def check_ufm_log_text(*, aspect: str, log_type: str, log_source: str, length: int = 100) -> dict[str, Any]:
    """Query a UFM text log endpoint and emit the observability contract."""
    result = _base_result(aspect)
    probes = {
        "log_endpoints_checked": 0,
        "log_source": "",
        "entry_count": 0,
        "latest_timestamp": "",
    }

    if not ufm_configured():
        error = "UFM access is not configured; set UFM_ADDRESS and UFM_TOKEN or UFM_USERNAME/UFM_PASSWORD"
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    try:
        auth = resolve_ufm_auth()
        content = get_log_text(auth, log_type, length=length)
    except HTTPError as e:
        error = describe_http_error(e)
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result
    except (URLError, UfmAuthError) as e:
        error = describe_url_error(e) if isinstance(e, URLError) else str(e)
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    entry_count, latest_timestamp = _parse_log_lines(content)
    probes = {
        "log_endpoints_checked": 1,
        "log_source": log_source,
        "entry_count": entry_count,
        "latest_timestamp": latest_timestamp,
    }
    result["tests"]["log_endpoint_reachable"] = _passed("UFM log endpoint reachable", probes)
    result["tests"]["log_source_present"] = _passed(f"UFM log source present: {log_source}", probes)
    result["tests"]["log_entries_queryable"] = _passed(f"{entry_count} UFM log entries returned", probes)
    result["success"] = True
    return result


def _check_switch_logs(aspect: str) -> dict[str, Any]:
    """Emit provider-hidden evidence for customer-inaccessible switch logs."""
    result = _base_result(aspect)
    result["success"] = True
    result["tests"] = {
        name: _provider_hidden(
            name,
            probe_field="switches_checked",
            message=NICO_SWITCH_LOGS_HIDDEN_MESSAGE,
        )
        for name in ASPECT_TESTS[aspect]
    }
    return result


def main() -> int:
    """Run the selected NICo observability probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="NICo observability log availability test")
    parser.add_argument(
        "--aspect",
        required=True,
        choices=sorted(ASPECT_TESTS),
        help="Observability aspect to test",
    )
    parser.add_argument("--page-size", type=int, default=10, help="UFM event history page size")
    args = parser.parse_args()

    if args.page_size < 1:
        print(
            json.dumps(
                {
                    "success": False,
                    "platform": "observability",
                    "test_name": args.aspect,
                    "error": "--page-size must be greater than 0",
                },
                indent=2,
            )
        )
        return 1

    result = _base_result(args.aspect)

    if DEMO_MODE:
        probes = dict(DEMO_PROBES[args.aspect])
        result["tests"] = {name: {"passed": True, "probes": probes} for name in ASPECT_TESTS[args.aspect]}
        result["success"] = True
    elif args.aspect == "ufm_event_logs":
        result = check_ufm_event_logs(page_size=args.page_size)
    elif args.aspect == "fabric_manager_logs":
        result = check_ufm_log_text(
            aspect="fabric_manager_logs",
            log_type="UFM",
            log_source="ufm-fabric-manager",
        )
    elif args.aspect == "subnet_manager_logs":
        result = check_ufm_log_text(
            aspect="subnet_manager_logs",
            log_type="SM",
            log_source="ufm-subnet-manager",
        )
    else:
        result = _check_switch_logs(args.aspect)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
