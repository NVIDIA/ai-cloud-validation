#!/usr/bin/env python3
"""sdn_logging_test — Armada Bridge network suite, test phase.

Validates SDN log queryability for a given log type (fault / perf / audit).

SdnHardwareFaultLoggingCheck requires:
  tests: {logging_endpoint_reachable, fault_event_source_queryable,
          log_destination_configured, event_schema_valid}
  evidence: log_destination, recent_event_count

SdnLatencyPerfLoggingCheck requires:
  tests: {metrics_endpoint_reachable, performance_metric_present,
          packet_metric_present, samples_recent}
  evidence: telemetry_namespace, sample_window_seconds, probe_resource_id

SdnFilterAuditTrailCheck requires:
  tests: {audit_endpoint_reachable, create_rule_logged, modify_rule_logged,
          delete_rule_logged, audit_event_has_required_fields, cleanup}
  evidence: trail_id, actor_field, target_rule_id

Output varies by --log-type (fault | perf | audit).
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient  # noqa: F401 — used in the live impl block
from common.errors import handle_bridge_errors

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_DEMO_BY_LOG_TYPE: dict[str, dict[str, Any]] = {
    "fault": {
        "tests": {
            "logging_endpoint_reachable": {"passed": True},
            "fault_event_source_queryable": {"passed": True},
            "log_destination_configured": {"passed": True},
            "event_schema_valid": {"passed": True},
        },
        "log_destination": "demo-log-dest",
        "recent_event_count": 0,
    },
    "perf": {
        "tests": {
            "metrics_endpoint_reachable": {"passed": True},
            "performance_metric_present": {"passed": True},
            "packet_metric_present": {"passed": True},
            "samples_recent": {"passed": True},
        },
        "telemetry_namespace": "demo-telemetry",
        "sample_window_seconds": 300,
        "probe_resource_id": "demo-vpc-0001",
    },
    "audit": {
        "tests": {
            "audit_endpoint_reachable": {"passed": True},
            "create_rule_logged": {"passed": True},
            "modify_rule_logged": {"passed": True},
            "delete_rule_logged": {"passed": True},
            "audit_event_has_required_fields": {"passed": True},
            "cleanup": {"passed": True},
        },
        "trail_id": "demo-trail-0001",
        "actor_field": "demo-actor",
        "target_rule_id": "demo-sg-rule-0001",
    },
}


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--log-type", required=True, choices=["fault", "perf", "audit"])
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update({"success": True, "platform": "network"})
        result.update(_DEMO_BY_LOG_TYPE[args.log_type])
    else:
        raise NotImplementedError(
            "sdn_logging_test: uncomment the Bridge implementation block. "
            "Use BridgeClient.from_env() to validate SDN log queryability."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
