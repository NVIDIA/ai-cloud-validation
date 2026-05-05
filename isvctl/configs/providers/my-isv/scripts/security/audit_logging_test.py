#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Audit logging test - TEMPLATE (replace with your platform implementation).

Verifies that a management API call creates an audit-log entry with required
metadata, and that audit logs are retained for at least 30 days. Covers
SEC08-01 and SEC08-02.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "audit_logging_test",
    "event_name": "<management API verb>",
    "request_id": "<request id or correlation id>",
    "audit_log_destination": "<audit log destination>",
    "minimum_retention_days": 30,
    "tests": {
      "audit_log_entry_found":              {"passed": true},
      "audit_log_event_name_matches":       {"passed": true},
      "audit_log_event_time_in_window":     {"passed": true},
      "audit_log_user_identity_present":    {"passed": true},
      "audit_log_source_ip_present":        {"passed": true},
      "audit_log_user_agent_matches":       {"passed": true},
      "audit_log_region_matches":           {"passed": true},
      "audit_log_event_source_matches":     {"passed": true},
      "audit_log_trail_logging_enabled":    {"passed": true},
      "audit_log_retention_at_least_30_days": {"passed": true}
    }
  }

Usage:
    python audit_logging_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Audit logging test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Audit logging test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "audit_logging_test",
        "event_name": "",
        "request_id": "",
        "audit_log_destination": "",
        "minimum_retention_days": 0,
        "tests": {
            "audit_log_entry_found": {"passed": False},
            "audit_log_event_name_matches": {"passed": False},
            "audit_log_event_time_in_window": {"passed": False},
            "audit_log_user_identity_present": {"passed": False},
            "audit_log_source_ip_present": {"passed": False},
            "audit_log_user_agent_matches": {"passed": False},
            "audit_log_region_matches": {"passed": False},
            "audit_log_event_source_matches": {"passed": False},
            "audit_log_trail_logging_enabled": {"passed": False},
            "audit_log_retention_at_least_30_days": {"passed": False},
        },
    }

    # TODO: Replace this block with your platform's SEC08 test.
    #
    # Suggested shape:
    #   marker = unique_probe_marker()
    #   response = management_client.call_read_only_api(user_agent_suffix=marker)
    #   event = audit_log.lookup(
    #       event_name=response.operation,
    #       request_id=response.request_id,
    #       user_agent_suffix=marker,
    #       timeout_seconds=600,
    #   )
    #   assert event.name == response.operation
    #   assert event.time is close to response.time
    #   assert event.identity and event.source_ip and event.user_agent
    #   assert audit_log_destination.retention_days is None or retention_days >= 30
    #
    # If logging is disabled or the event has not propagated within the poll
    # budget, emit the check-specific ``audit_log_*_skipped`` fields plus a
    # reason and exit 0.

    if DEMO_MODE:
        result["event_name"] = "DescribeControlPlane"
        result["request_id"] = "demo-sec08-request"
        result["audit_log_destination"] = "demo-audit-log-store"
        result["minimum_retention_days"] = 90
        result["tests"] = {
            "audit_log_entry_found": {"passed": True, "message": "demo: matching audit entry found"},
            "audit_log_event_name_matches": {"passed": True},
            "audit_log_event_time_in_window": {"passed": True},
            "audit_log_user_identity_present": {"passed": True},
            "audit_log_source_ip_present": {"passed": True},
            "audit_log_user_agent_matches": {"passed": True},
            "audit_log_region_matches": {"passed": True},
            "audit_log_event_source_matches": {"passed": True},
            "audit_log_trail_logging_enabled": {"passed": True, "message": "demo: audit logging enabled"},
            "audit_log_retention_at_least_30_days": {"passed": True, "message": "demo: retention is 90 days"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's audit logging test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
