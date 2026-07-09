#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Telemetry delivery latency test template."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

ASPECT_TESTS: list[str] = [
    "telemetry_endpoint_reachable",
    "delivery_sample_present",
    "delivery_within_threshold",
]

DEMO_PROBES: dict[str, Any] = {
    "telemetry_source": "demo-telemetry-pipeline",
    "observed_delivery_seconds": 42,
    "max_delivery_seconds": 120,
    "sample_count": 3,
    "latest_timestamp": "2026-05-20T13:21:00Z",
}


def _base_result() -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": "telemetry_delivery_latency",
        "tests": {name: {"passed": False} for name in ASPECT_TESTS},
    }


def main() -> int:
    """Run the telemetry delivery latency template probe."""
    parser = argparse.ArgumentParser(description="Telemetry delivery latency test (template)")
    parser.add_argument("--region", default="")
    parser.add_argument("--network-id", default="")
    parser.add_argument("--max-delivery-seconds", type=int, default=120)
    args = parser.parse_args()

    result = _base_result()
    probes = dict(DEMO_PROBES)
    probes["max_delivery_seconds"] = args.max_delivery_seconds

    if DEMO_MODE:
        result["tests"] = {name: {"passed": True, "probes": probes} for name in ASPECT_TESTS}
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's telemetry delivery probe"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
