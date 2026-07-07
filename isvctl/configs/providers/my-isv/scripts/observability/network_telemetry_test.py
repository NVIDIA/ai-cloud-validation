#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Network-plane telemetry availability test template."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

ASPECT_TESTS: dict[str, list[str]] = {
    "north_south_network_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "east_west_network_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "management_network_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "nvswitch_fabric_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "host_nic_network_telemetry": [
        "telemetry_endpoint_reachable",
        "nic_metrics_present",
        "samples_recent",
    ],
}

NETWORK_PLANES = {
    "north_south_network_telemetry": "north_south",
    "east_west_network_telemetry": "east_west",
    "management_network_telemetry": "management",
    "nvswitch_fabric_telemetry": "nvswitch_fabric",
    "host_nic_network_telemetry": "host_nic",
}

DEMO_PROBES: dict[str, dict[str, Any]] = {
    "north_south_network_telemetry": {
        "telemetry_source": "demo-north-south-telemetry",
        "metric_names": ["ingress_bytes", "egress_bytes"],
        "sample_count": 4,
        "latest_timestamp": "2026-05-20T13:21:00Z",
    },
    "east_west_network_telemetry": {
        "telemetry_source": "demo-east-west-telemetry",
        "metric_names": ["gpu_interconnect_bytes"],
        "sample_count": 3,
        "latest_timestamp": "2026-05-20T13:20:00Z",
    },
    "management_network_telemetry": {
        "telemetry_source": "demo-management-telemetry",
        "metric_names": ["management_packets"],
        "sample_count": 2,
        "latest_timestamp": "2026-05-20T13:19:00Z",
    },
    "nvswitch_fabric_telemetry": {
        "telemetry_source": "demo-nvswitch-telemetry",
        "metric_names": ["nvswitch_port_errors"],
        "sample_count": 1,
        "latest_timestamp": "2026-05-20T13:18:00Z",
    },
    "host_nic_network_telemetry": {
        "telemetry_source": "demo-host-nic-telemetry",
        "metric_names": ["nic_rx_bytes", "nic_tx_bytes"],
        "nics_checked": 2,
        "sample_count": 5,
        "latest_timestamp": "2026-05-20T13:17:00Z",
    },
}


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "network_plane": NETWORK_PLANES[aspect],
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
    }


def main() -> int:
    """Run the selected network telemetry template probe."""
    parser = argparse.ArgumentParser(description="Network telemetry availability test (template)")
    parser.add_argument("--region", default="")
    parser.add_argument("--network-id", default="")
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_TESTS))
    args = parser.parse_args()

    result = _base_result(args.aspect)

    if DEMO_MODE:
        probes = dict(DEMO_PROBES[args.aspect])
        result["tests"] = {name: {"passed": True, "probes": probes} for name in ASPECT_TESTS[args.aspect]}
        result["success"] = True
    else:
        result["error"] = f"Not implemented - replace with your platform's network telemetry probe for {args.aspect}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
