#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS NVLink telemetry availability probes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.errors import handle_aws_errors

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

HIDDEN_ASPECT_PROBE_FIELDS: dict[str, str] = {
    "gpu_nvlink_telemetry": "links_checked",
    "switch_nvlink_telemetry": "ports_checked",
}

AWS_NO_CUSTOMER_NVLINK_MESSAGE = (
    "AWS EC2/EKS tenants do not receive customer-accessible NVLink telemetry from GPU or switch planes"
)


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
    }


def _provider_hidden(test_name: str, *, probe_field: str, region: str) -> dict[str, Any]:
    """Build a passing provider-hidden subtest result."""
    return {
        "passed": True,
        "provider_hidden": True,
        "probes": {probe_field: 0, "telemetry_source": "", "metric_names": []},
        "message": (
            f"{test_name}: {AWS_NO_CUSTOMER_NVLINK_MESSAGE} in region {region}; NVLink plane is provider-owned."
        ),
    }


def check_provider_hidden_aspect(aspect: str, *, region: str) -> dict[str, Any]:
    """Emit AWS provider-hidden evidence for tenant-inaccessible NVLink telemetry."""
    result = _base_result(aspect)
    result["success"] = True
    probe_field = HIDDEN_ASPECT_PROBE_FIELDS[aspect]
    result["tests"] = {
        name: _provider_hidden(name, probe_field=probe_field, region=region) for name in ASPECT_TESTS[aspect]
    }
    return result


@handle_aws_errors
def main() -> int:
    """Run the selected AWS NVLink telemetry probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="AWS NVLink telemetry availability test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_TESTS))
    args = parser.parse_args()

    result = check_provider_hidden_aspect(args.aspect, region=args.region)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
