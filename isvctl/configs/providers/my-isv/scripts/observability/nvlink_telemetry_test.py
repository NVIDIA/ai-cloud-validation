#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NVLink telemetry availability test template."""

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
    "gpu_nvlink_telemetry": [
        "telemetry_endpoint_reachable",
        "link_metrics_present",
        "samples_recent",
    ],
    "switch_nvlink_telemetry": [
        "telemetry_endpoint_reachable",
        "port_metrics_present",
        "samples_recent",
    ],
}

DEMO_PROBES: dict[str, dict[str, Any]] = {
    "gpu_nvlink_telemetry": {
        "telemetry_source": "demo-gpu-nvlink",
        "links_checked": 4,
        "metric_names": ["nvlink.tx_bytes", "nvlink.rx_bytes", "nvlink.bandwidth_util"],
        "sample_count": 6,
        "latest_timestamp": "2026-05-20T13:19:00Z",
    },
    "switch_nvlink_telemetry": {
        "telemetry_source": "demo-switch-nvlink",
        "ports_checked": 8,
        "metric_names": ["nvlink.port.rx_errors", "nvlink.port.tx_counters"],
        "sample_count": 5,
        "latest_timestamp": "2026-05-20T13:18:00Z",
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
    """Run the selected NVLink telemetry template probe."""
    parser = argparse.ArgumentParser(description="NVLink telemetry availability test (template)")
    parser.add_argument("--region", default="")
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_TESTS))
    args = parser.parse_args()

    result = _base_result(args.aspect)

    if DEMO_MODE:
        probes = dict(DEMO_PROBES[args.aspect])
        result["tests"] = {name: {"passed": True, "probes": probes} for name in ASPECT_TESTS[args.aspect]}
        result["success"] = True
    else:
        result["error"] = f"Not implemented - replace with your platform's NVLink telemetry probe for {args.aspect}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
