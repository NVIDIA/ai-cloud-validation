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

"""Least-privilege and minimal-role enforcement (SEC04-01, SEC04-02).

Creates a service account and a throwaway second project (B), then grants the
account VIEWER - read-only - on the run's project (A) only. As that account:

  policy_dimensions_user_based       GET /projects/A is 403 before the grant and
                                     200 after it: access follows the identity's
                                     own binding
  policy_dimensions_resource_based   the same account gets 200 on A, 403 on B
  policy_dimensions_allowed_action_succeeds  GET /projects/A answers 200
  out_of_scope_compute_denied        POST /projects/A/compute/bms/<unknown>/power-on is 403
  out_of_scope_storage_denied        POST /projects/A/storage/fs is 403
  out_of_scope_network_denied        POST /projects/A/network/vpcs is 403

The API checks the permission before looking the target up or validating the
body, so a denial is 403. The out-of-scope probes cannot change anything even
if they were allowed: the BM does not exist and the create bodies carry an empty
name the API rejects (400) - either answer counts as not denied. The account and
project B are recorded as soon as each is created and deleted by the teardown step.

Usage:
    python least_privilege_test.py

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "least_privilege_test",
    "test_identity": "service-account.xxx",
    "allowed_resource": "project.A",
    "denied_resource": "project.B",
    "role": "VIEWER",
    "created_service_account_ids": ["service-account.xxx"],
    "created_project_ids": ["project.B"],
    "tests": {"policy_dimensions_user_based": {"passed": true}, ..., "out_of_scope_network_denied": {"passed": true}}
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import service_accounts
from common.firebird_client import FirebirdClient, log
from common.service_accounts import MINIMAL_PROJECT_ROLE, status_of

KEYS = (
    "policy_dimensions_user_based",
    "policy_dimensions_resource_based",
    "policy_dimensions_allowed_action_succeeds",
    "out_of_scope_compute_denied",
    "out_of_scope_storage_denied",
    "out_of_scope_network_denied",
)
# A well-formed BM ID that names no server: the power-on probe cannot act on anything.
ABSENT_BM_ID = "bm.00000000000000000000000000"


def outcome(passed: bool, detail: str) -> dict[str, Any]:
    """Return a subtest result carrying ``detail`` as its message or error."""
    return {"passed": True, "message": detail} if passed else {"passed": False, "error": detail}


def denied(status: int, action: str) -> dict[str, Any]:
    """Return a subtest that passes only when ``action`` was refused with 403."""
    if status == 403:
        return outcome(True, f"{action}: HTTP 403")
    return outcome(False, f"{action}: HTTP {status}, expected 403 (the request passed authorization)")


def main() -> int:
    """Set up the scoped account, probe allowed and denied calls, and emit JSON.

    Returns:
        0 when every probe passes, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird least-privilege test")
    parser.add_argument("--retries", type=int, default=5, help="Token attempts while a new client propagates")
    parser.add_argument("--wait", type=float, default=5, help="Seconds between token attempts")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    created_accounts: list[str] = []
    created_projects: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "least_privilege_test",
        "role": MINIMAL_PROJECT_ROLE,
        "created_service_account_ids": created_accounts,
        "created_project_ids": created_projects,
        "tests": tests,
    }
    try:
        admin = FirebirdClient()
        project_a = admin.project_id
        body = {"name": service_accounts.unique_name("isv-lp"), "description": "isvctl least-privilege probe"}
        project_b = (admin.request("POST", "/projects", body).get("project") or {}).get("id") or ""
        if not project_b:
            raise RuntimeError("POST /projects returned no project ID")
        created_projects.append(project_b)
        log(f"  created project {project_b}")

        account = service_accounts.create(admin, "isv-sec-lp")
        created_accounts.append(account.id)
        service_accounts.require_credentials(account)
        result.update(test_identity=account.id, allowed_resource=project_a, denied_resource=project_b)

        sa, _ = service_accounts.login(account.client_id, account.client_secret, args.retries, args.wait)

        def read_a() -> dict[str, Any]:
            """Read project A as the scoped account."""
            return sa.request("GET", f"/projects/{quote(project_a)}")

        before = status_of(read_a)

        service_accounts.set_project_roles(admin, project_a, account.id, [MINIMAL_PROJECT_ROLE])
        after = status_of(read_a)
        other = status_of(lambda: sa.request("GET", f"/projects/{quote(project_b)}"))

        tests["policy_dimensions_user_based"] = outcome(
            before == 403 and after == 200,
            f"GET {project_a}: HTTP {before} before the {MINIMAL_PROJECT_ROLE} grant, {after} after",
        )
        tests["policy_dimensions_resource_based"] = outcome(
            after == 200 and other == 403,
            f"HTTP {after} on {project_a} (granted), {other} on {project_b} (not granted)",
        )
        tests["policy_dimensions_allowed_action_succeeds"] = outcome(after == 200, f"GET {project_a}: HTTP {after}")

        base = f"/projects/{quote(project_a)}"
        tests["out_of_scope_compute_denied"] = denied(
            status_of(lambda: sa.request("POST", f"{base}/compute/bms/{ABSENT_BM_ID}/power-on", {})), "BM power-on"
        )
        tests["out_of_scope_storage_denied"] = denied(
            status_of(lambda: sa.request("POST", f"{base}/storage/fs", {"name": ""})), "filesystem create"
        )
        tests["out_of_scope_network_denied"] = denied(
            status_of(lambda: sa.request("POST", f"{base}/network/vpcs", {"name": ""})), "VPC create"
        )
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    for key in KEYS:
        tests.setdefault(key, outcome(False, result.get("error", "not run")))
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
