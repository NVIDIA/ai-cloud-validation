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

"""Check cloud API connectivity and health.

Provider-agnostic template - replace the TODO section with your platform's
API client calls (e.g. OpenStack SDK, GCP client, Azure SDK, etc.).

Required JSON output:
{
    "success":    bool    - true if authentication and at least core services reachable,
    "platform":   str     - "control_plane",
    "account_id": str     - authenticated identity / account / project ID,
    "tests": {
        "auth":          {"passed": bool},
        "<service_name>": {"passed": bool}
        ...one entry per service checked...
    },
    "error": str  - (optional) error message, present when success is false
}

Usage:
    python check_api.py --region <region> --services compute,storage,identity

AWS reference implementation:
    ../aws/control-plane/check_api.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Check cloud API health and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Check cloud API health")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    parser.add_argument(
        "--services",
        default="compute,storage,identity",
        help="Comma-separated list of services to probe",
    )
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",")]

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "account_id": "",
        "tests": {},
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. Authenticate to your cloud API (SDK client, token, etc.)     ║
    # ║  2. Retrieve the caller identity / account ID                    ║
    # ║     -> result["account_id"] = "<your-account-id>"                ║
    # ║  3. For each service in `services`:                              ║
    # ║     a. Call a lightweight read-only endpoint                     ║
    # ║     b. Record the result:                                        ║
    # ║        result["tests"]["<service>"] = {"passed": True/False}     ║
    # ║  4. Set result["success"] = True if auth passed                  ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["account_id"] = "dummy-account-123"

        for service in services:
            result["tests"][service] = {"passed": True}
        result["tests"]["auth"] = {"passed": True}
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's API health-check logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
