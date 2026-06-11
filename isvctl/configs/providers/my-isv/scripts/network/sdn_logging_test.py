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

"""SDN logging test - TEMPLATE (replace with your platform implementation).

The --aspect flag selects which SDN09 requirement to probe:

  hardware_faults:
    tests: {logging_endpoint_reachable, fault_event_source_queryable,
            log_destination_configured, event_schema_valid}
    evidence: log_destination, recent_event_count

  latency_perf:
    tests: {metrics_endpoint_reachable, performance_metric_present,
            packet_metric_present, samples_recent}
    evidence: telemetry_namespace, sample_window_seconds, probe_resource_id

  audit_trail:
    tests: {audit_endpoint_reachable, create_rule_logged,
            modify_rule_logged, delete_rule_logged,
            audit_event_has_required_fields, cleanup}
    evidence: trail_id, actor_field, target_rule_id

Usage:
    python sdn_logging_test.py --region <region> --vpc-id <network-id> --aspect hardware_faults
    python sdn_logging_test.py --region <region> --vpc-id <network-id> --aspect latency_perf
    python sdn_logging_test.py --region <region> --vpc-id <network-id> --aspect audit_trail
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

ASPECT_TESTS: dict[str, list[str]] = {
    "hardware_faults": [
        "logging_endpoint_reachable",
        "fault_event_source_queryable",
        "log_destination_configured",
        "event_schema_valid",
    ],
    "latency_perf": [
        "metrics_endpoint_reachable",
        "performance_metric_present",
        "packet_metric_present",
        "samples_recent",
    ],
    "audit_trail": [
        "audit_endpoint_reachable",
        "create_rule_logged",
        "modify_rule_logged",
        "delete_rule_logged",
        "audit_event_has_required_fields",
        "cleanup",
    ],
}

ASPECT_STEP_NAMES: dict[str, str] = {
    "hardware_faults": "sdn_hardware_fault_logging",
    "latency_perf": "sdn_latency_perf_logging",
    "audit_trail": "sdn_filter_audit_trail",
}

DEMO_EVIDENCE: dict[str, dict[str, Any]] = {
    "hardware_faults": {
        "log_destination": "demo-sdn-log-destination",
        "recent_event_count": 1,
    },
    "latency_perf": {
        "telemetry_namespace": "demo/sdn",
        "sample_window_seconds": 60,
        "probe_resource_id": "demo-sdn-probe",
    },
    "audit_trail": {
        "trail_id": "demo-audit-trail",
        "actor_field": "demo.actor",
        "target_rule_id": "demo-filter-rule",
    },
}


def main() -> int:
    """Run the selected SDN logging template probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="SDN logging test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--vpc-id", required=True, help="Network/VPC identifier to inspect")
    parser.add_argument(
        "--aspect",
        required=True,
        choices=["hardware_faults", "latency_perf", "audit_trail"],
        help="SDN logging aspect to test",
    )
    args = parser.parse_args()

    test_names = ASPECT_TESTS[args.aspect]
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": ASPECT_STEP_NAMES[args.aspect],
        "region": args.region,
        "network_id": args.vpc_id,
        "aspect": args.aspect,
        "tests": {name: {"passed": False} for name in test_names},
    }

    # TODO: Replace this block with your platform's SDN-controller logging
    # introspection for the selected aspect.
    if DEMO_MODE:
        result["tests"] = {name: {"passed": True} for name in test_names}
        result.update(DEMO_EVIDENCE[args.aspect])
        result["success"] = True
    else:
        result["error"] = f"Not implemented - replace with your platform's SDN logging probe for {args.aspect}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
