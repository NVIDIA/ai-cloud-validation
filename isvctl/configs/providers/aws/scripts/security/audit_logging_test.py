#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Emit structured SEC08 audit logging skips for the AWS reference provider.

Audit-log implementation details are environment specific: organizations may
use CloudTrail, CloudTrail Lake, SIEM forwarding, or another control-plane log
pipeline. The AWS reference keeps this check intentionally small and reports a
structured skip rather than baking in a CloudTrail-specific reference probe.
"""

import argparse
import json
import os
import sys
from typing import Any

TEST_NAME = "audit_logging_test"
AUDIT_ENTRY_TEST_KEYS = (
    "audit_log_entry_found",
    "audit_log_event_name_matches",
    "audit_log_event_time_in_window",
    "audit_log_user_identity_present",
    "audit_log_source_ip_present",
    "audit_log_user_agent_matches",
    "audit_log_region_matches",
    "audit_log_event_source_matches",
)
AUDIT_RETENTION_TEST_KEYS = (
    "audit_log_trail_logging_enabled",
    "audit_log_retention_at_least_30_days",
)
SKIP_REASON = "AWS audit logging validation is environment specific"


def _base_result() -> dict[str, Any]:
    """Return the provider-neutral SEC08 result envelope."""
    return {
        "success": True,
        "platform": "security",
        "test_name": TEST_NAME,
        "audit_log_entry_skipped": True,
        "audit_log_entry_skip_reason": SKIP_REASON,
        "audit_log_retention_skipped": True,
        "audit_log_retention_skip_reason": SKIP_REASON,
        "tests": {
            key: {"passed": True, "skipped": True, "skip_reason": SKIP_REASON}
            for key in (*AUDIT_ENTRY_TEST_KEYS, *AUDIT_RETENTION_TEST_KEYS)
        },
    }


def main() -> int:
    """Emit structured skip JSON for SEC08 audit logging and retention."""
    parser = argparse.ArgumentParser(description="Audit logging and retention test (SEC08-01/02)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--lookup-timeout-seconds",
        type=int,
        default=600,
        help="Accepted for compatibility; not used by the AWS reference skip.",
    )
    parser.parse_args()

    print(json.dumps(_base_result(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
