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
    parser.parse_args()

    print(json.dumps(_base_result(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
