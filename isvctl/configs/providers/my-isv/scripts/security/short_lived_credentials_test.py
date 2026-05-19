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

"""Short-lived credentials test - TEMPLATE (replace with your platform implementation).

Verifies that workloads and nodes receive credentials with a finite expiry
that does not exceed a configured upper bound. Covers SEC02-01.

The validation expects two distinct issuance surfaces to be probed:

  * Node-side: credentials a host/instance role acquires from the platform
    identity service.
  * Workload-side: credentials an in-cluster workload acquires through the
    workload identity flow.

When neither issuance path is available in the current environment the
script may emit a structured ``skipped`` payload (top-level
``skipped: true`` plus a ``skip_reason``) and exit 0; the validation will
treat that as a clean skip rather than a failure.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "short_lived_credentials_test",
    "node_credential_method": "<short label, e.g. instance-metadata role>",
    "workload_credential_method": "<short label, e.g. workload-identity>",
    "node_credential_ttl_seconds": <int>,
    "workload_credential_ttl_seconds": <int>,
    "max_ttl_seconds": <int>,
    "tests": {
      "node_credential_has_expiry":           {"passed": true},
      "node_credential_ttl_within_bound":     {"passed": true},
      "workload_credential_has_expiry":       {"passed": true},
      "workload_credential_ttl_within_bound": {"passed": true}
    }
  }

Usage:
    python short_lived_credentials_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
DEFAULT_MAX_TTL_SECONDS = 43200  # 12h upper bound


def main() -> int:
    """Short-lived credentials test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Short-lived credentials test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument(
        "--max-ttl-seconds",
        type=int,
        default=DEFAULT_MAX_TTL_SECONDS,
        help="Upper bound on credential TTL (default: 43200 = 12h)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "short_lived_credentials_test",
        "node_credential_method": "",
        "workload_credential_method": "",
        "node_credential_ttl_seconds": 0,
        "workload_credential_ttl_seconds": 0,
        "max_ttl_seconds": args.max_ttl_seconds,
        "tests": {
            "node_credential_has_expiry": {"passed": False},
            "node_credential_ttl_within_bound": {"passed": False},
            "workload_credential_has_expiry": {"passed": False},
            "workload_credential_ttl_within_bound": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's short-lived       ║
    # ║  credentials test.                                               ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    node_creds = mint_node_credentials(region)                    ║
    # ║    assert node_creds.expires_at is not None                      ║
    # ║    node_ttl = (node_creds.expires_at - now()).total_seconds()    ║
    # ║    assert 0 < node_ttl <= max_ttl_seconds                        ║
    # ║                                                                  ║
    # ║    wl_creds = mint_workload_credentials(region)                  ║
    # ║    assert wl_creds.expires_at is not None                        ║
    # ║    wl_ttl = (wl_creds.expires_at - now()).total_seconds()        ║
    # ║    assert 0 < wl_ttl <= max_ttl_seconds                          ║
    # ║                                                                  ║
    # ║  If neither issuance path can be exercised in this environment,  ║
    # ║  emit ``skipped: true`` plus a ``skip_reason`` and exit 0.       ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["node_credential_method"] = "instance-role-token"
        result["workload_credential_method"] = "workload-identity-token"
        result["node_credential_ttl_seconds"] = 3600
        result["workload_credential_ttl_seconds"] = 3600
        result["tests"] = {
            "node_credential_has_expiry": {"passed": True},
            "node_credential_ttl_within_bound": {"passed": True},
            "workload_credential_has_expiry": {"passed": True},
            "workload_credential_ttl_within_bound": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's short-lived credentials test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
