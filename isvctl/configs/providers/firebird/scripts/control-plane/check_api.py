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

"""Firebird control-plane API health (CP03-01).

Two authenticated reads scoped to the run's account:
  identity  ``GET /users/me`` names the caller
  project   ``GET /projects/{p}`` returns the run's project; its ``tenantId`` is
            the account ID (``/users/me`` may list no tenants for a service
            account)

Usage:
    python check_api.py

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "account_id": "tenant.xxx",
    "tests": {"identity": {"passed": true, "latency_ms": 42.1}, "project": {"passed": true, "latency_ms": 38.0}}
}
"""

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log


def _timed(call: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], float]:
    """Run ``call`` and return its result with the elapsed milliseconds."""
    start = time.monotonic()
    payload = call()
    return payload, round((time.monotonic() - start) * 1000, 2)


def main() -> int:
    """Probe the identity and project endpoints and emit JSON.

    Returns:
        0 when both probes pass, 1 otherwise
    """
    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "control_plane", "account_id": "", "tests": tests}
    try:
        client = FirebirdClient()
        me, latency = _timed(lambda: client.request("GET", "/users/me"))
        tests["identity"] = {"passed": bool(me.get("userId")), "latency_ms": latency}
        project, latency = _timed(client.get_project)
        result["account_id"] = project["tenantId"]
        tests["project"] = {"passed": project.get("id") == client.project_id, "latency_ms": latency}
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    for key in ("identity", "project"):
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
