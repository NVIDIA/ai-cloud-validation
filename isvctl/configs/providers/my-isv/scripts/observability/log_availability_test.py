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

"""Observability log and telemetry availability test template.

The ``--aspect`` flag selects the observability surface to probe:

  vpc_flow_logs:
    tests: {flow_log_endpoint_reachable, flow_logs_configured,
            traffic_type_all, log_destination_accessible}
    probes: network_id, log_destination, traffic_type, sample_window_seconds

  host_syslogs:
    tests: {syslog_endpoint_reachable, host_log_source_present,
            entries_recent}
    probes: hosts_checked, log_source, entry_count, latest_timestamp

  bmc_sel_logs:
    tests: {sel_log_endpoint_reachable, sel_log_source_present,
            sel_entries_queryable}
    probes: bmc_endpoints_checked, log_source, entry_count, latest_timestamp

  bmc_gpu_telemetry:
    tests: {telemetry_endpoint_reachable, gpu_metrics_present,
            host_os_gap_identified, telemetry_samples_recent}
    probes: bmc_endpoints_checked, telemetry_endpoint, metric_names,
            host_os_unavailable_metrics, sample_count

  ufm_event_logs:
    tests: {event_log_endpoint_reachable, event_log_source_present,
            event_entries_queryable}
    probes: log_endpoints_checked, log_source, entry_count, latest_timestamp

  fabric_manager_logs:
    tests: {log_endpoint_reachable, log_source_present, log_entries_queryable}
    probes: log_endpoints_checked, log_source, entry_count, latest_timestamp

  subnet_manager_logs:
    tests: {log_endpoint_reachable, log_source_present, log_entries_queryable}
    probes: log_endpoints_checked, log_source, entry_count, latest_timestamp

  general_switch_logs:
    tests: {log_endpoint_reachable, switch_log_source_present,
            entries_queryable}
    probes: switches_checked, log_source, entry_count, latest_timestamp

  switch_syslogs:
    tests: {syslog_endpoint_reachable, switch_syslog_source_present,
            entries_recent}
    probes: switches_checked, log_source, entry_count, latest_timestamp

  switch_kernel_logs:
    tests: {log_endpoint_reachable, kernel_log_source_present,
            entries_queryable}
    probes: switches_checked, log_source, entry_count, latest_timestamp
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow importing provider-local helpers from scripts/common/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

ASPECT_TESTS: dict[str, list[str]] = {
    "vpc_flow_logs": [
        "flow_log_endpoint_reachable",
        "flow_logs_configured",
        "traffic_type_all",
        "log_destination_accessible",
    ],
    "host_syslogs": [
        "syslog_endpoint_reachable",
        "host_log_source_present",
        "entries_recent",
    ],
    "bmc_sel_logs": [
        "sel_log_endpoint_reachable",
        "sel_log_source_present",
        "sel_entries_queryable",
    ],
    "bmc_gpu_telemetry": [
        "telemetry_endpoint_reachable",
        "gpu_metrics_present",
        "host_os_gap_identified",
        "telemetry_samples_recent",
    ],
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
    "vpc_flow_logs": {
        "log_destination": "demo-vpc-flow-log-destination",
        "traffic_type": "ALL",
        "sample_window_seconds": 120,
    },
    "host_syslogs": {
        "hosts_checked": 2,
        "log_source": "demo-host-syslog",
        "entry_count": 12,
        "latest_timestamp": "2026-05-20T13:21:00Z",
    },
    "bmc_sel_logs": {
        "bmc_endpoints_checked": 1,
        "log_source": "demo-redfish-system-event-log",
        "entry_count": 1,
        "latest_timestamp": "2026-05-20T13:20:00Z",
    },
    "bmc_gpu_telemetry": {
        "bmc_endpoints_checked": 1,
        "telemetry_endpoint": "demo-redfish-telemetry-service",
        "metric_names": ["gpu.power_state", "gpu.remediation_state"],
        "host_os_unavailable_metrics": ["gpu.power_state", "gpu.remediation_state"],
        "sample_count": 4,
    },
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


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
    }


def main() -> int:
    """Run the selected observability template probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Observability log availability test (template)")
    parser.add_argument(
        "--region", default="", help="Cloud region (unused in the template; wire into your provider probe)"
    )
    parser.add_argument("--network-id", default="", help="Network/VPC identifier for network log probes")
    parser.add_argument(
        "--aspect",
        required=True,
        choices=sorted(ASPECT_TESTS),
        help="Observability aspect to test",
    )
    args = parser.parse_args()

    result = _base_result(args.aspect)

    # TODO: Replace demo output with your platform's log collector, Redfish,
    # BMC, or telemetry API probes for the selected aspect.
    if DEMO_MODE:
        probes = dict(DEMO_PROBES[args.aspect])
        if args.aspect == "vpc_flow_logs":
            probes["network_id"] = args.network_id or "demo-network"
        result["tests"] = {name: {"passed": True, "probes": probes} for name in ASPECT_TESTS[args.aspect]}
        result["success"] = True
    else:
        result["error"] = f"Not implemented - replace with your platform's observability probe for {args.aspect}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
