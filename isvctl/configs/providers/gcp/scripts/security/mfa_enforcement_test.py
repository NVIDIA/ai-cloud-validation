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

"""Verify administrative interfaces require multi-factor authentication (GCP).

Reads org 2-Step Verification (2SV) state via the Admin SDK Directory API
(googleapiclient.discovery directory_v1, users().list over the caller's own
organization, customer "my_customer") and derives the three platform-neutral
subtests the contract requires. Where AWS attests root MFA + per-IAM-user MFA
devices + an IAM MFA-deny policy, GCP human identity (2SV) lives in Cloud
Identity / Workspace and is read per user as isEnforcedIn2Sv.

Subtests (each derived from real org 2SV state, never hardcoded):

  1. admin_account_mfa:        every super-admin user has isEnforcedIn2Sv.
  2. interactive_access_mfa:   every in-scope human user has isEnforcedIn2Sv.
  3. programmatic_access_mfa:  org-level 2SV enforcement signal — human API/CLI
     access rides 2SV at the login session (every in-scope user enforced). This
     is the principal-access outcome; it does NOT attest machine service-account
     / token / API-key credentials, which 2SV does not gate.

The run credential typically lacks the admin.directory scope and is not a
Workspace admin. When Admin SDK Directory access is unavailable (or the
directory returns no users) org 2SV state cannot be read, so the stub emits a
structured skip (success:true, skipped:true, skip_reason naming the missing
admin.directory.user.readonly access) rather than a fabricated pass -- a skip,
not a False subtest, because the step must not redden the test phase on the
documented-unavailable environment. The provider config additionally excludes
this check (exclude.tests) until a Workspace-admin credential with directory-read
access is provisioned; only then does the stub do real work and attest the three
subtests from live per-user 2SV enforcement state (never hardcoded).

Usage:
    python3 mfa_enforcement_test.py --region us-central1 --project my-project

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "mfa_enforcement",
    "interfaces_checked": 3,
    "tests": {
        "admin_account_mfa": {"passed": true, ...},
        "interactive_access_mfa": {"passed": true, ...},
        "programmatic_access_mfa": {"passed": true, ...}
    }
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors

# Caller's own organization. The Directory API accepts the literal
# "my_customer" alias to mean the customer that owns the authenticated account.
_MY_CUSTOMER = "my_customer"
_USERS_PAGE_SIZE = 200


def _list_directory_users(service: Any) -> list[dict[str, Any]]:
    """Page through every user in the caller's organization.

    Raises on transport / authorization errors so the caller can surface the
    missing-access outcome rather than treating an empty list as a pass.
    """
    users: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        response = (
            service.users()
            .list(
                customer=_MY_CUSTOMER,
                maxResults=_USERS_PAGE_SIZE,
                pageToken=page_token,
                projection="full",
            )
            .execute()
        )
        users.extend(response.get("users", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return users


def _evaluate_2sv(users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Derive the three MFA subtests from real per-user 2SV enforcement state.

    ``isEnforcedIn2Sv`` is the org-mandatory enforcement signal (2SV required to
    sign in), distinct from ``isEnrolledIn2Sv`` (the user chose to enroll).
    Enforcement is the control the contract attests. The caller guarantees a
    non-empty user list (an empty directory is handled as a structured skip).
    """
    admins_without_2sv: list[str] = []
    users_without_2sv: list[str] = []
    admin_count = 0
    for user in users:
        email = user.get("primaryEmail", "<unknown>")
        enforced = bool(user.get("isEnforcedIn2Sv", False))
        is_admin = bool(user.get("isAdmin", False)) or bool(user.get("isDelegatedAdmin", False))
        if is_admin:
            admin_count += 1
            if not enforced:
                admins_without_2sv.append(email)
        if not enforced:
            users_without_2sv.append(email)

    tests: dict[str, dict[str, Any]] = {}

    if admin_count == 0:
        tests["admin_account_mfa"] = {
            "passed": False,
            "error": "No super-admin user found to attest mandatory 2SV enforcement",
        }
    elif admins_without_2sv:
        tests["admin_account_mfa"] = {
            "passed": False,
            "error": f"Super-admin users without enforced 2SV: {admins_without_2sv}",
        }
    else:
        tests["admin_account_mfa"] = {
            "passed": True,
            "message": f"{admin_count} super-admin user(s) have 2SV enforced",
        }

    if users_without_2sv:
        verdict = {
            "passed": False,
            "error": f"{len(users_without_2sv)}/{len(users)} users without enforced 2SV: {users_without_2sv}",
        }
    else:
        verdict = {
            "passed": True,
            "message": f"all {len(users)} in-scope users have 2SV enforced",
        }
    # interactive_access_mfa and programmatic_access_mfa share the same
    # org-level enforcement signal: human API/CLI access rides 2SV at the login
    # session, so principal programmatic access is gated by the same enforcement
    # as interactive login.
    tests["interactive_access_mfa"] = dict(verdict)
    tests["programmatic_access_mfa"] = dict(verdict)
    return tests


@handle_gcp_errors
def main() -> int:
    """Run MFA enforcement checks over org 2SV state and emit JSON result."""
    parser = argparse.ArgumentParser(description="GCP MFA enforcement test")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "mfa_enforcement",
        "interfaces_checked": 0,
        "tests": {
            "admin_account_mfa": {"passed": False},
            "interactive_access_mfa": {"passed": False},
            "programmatic_access_mfa": {"passed": False},
        },
    }

    try:
        # resolve_project validates a usable GCP identity is present even though
        # the Directory API is org- not project-scoped.
        resolve_project(args.project)

        from googleapiclient import discovery
        from googleapiclient.errors import HttpError

        service = discovery.build("admin", "directory_v1", cache_discovery=False)
        try:
            users = _list_directory_users(service)
        except HttpError as e:
            # 403 (no admin.directory scope / not a Workspace admin) or 401 is the
            # documented-unavailable environment. Org 2SV state cannot be read, so
            # emit a structured skip (rc=0): the step must not redden the test
            # phase, and the provider config excludes the validator until a
            # Workspace-admin credential with directory-read access is provisioned.
            status = getattr(getattr(e, "resp", None), "status", None)
            if status not in {401, 403}:
                # Bad requests, missing endpoints, quota failures, and service
                # outages are operational failures, not evidence that the
                # Directory surface is structurally unavailable.
                raise
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = (
                f"Admin SDK Directory access unavailable (HTTP {status}); the run "
                "credential needs Cloud Identity / Workspace admin directory-read "
                "(admin.directory.user.readonly) to attest org 2SV state"
            )
            print(json.dumps(result, indent=2))
            return 0

        if not users:
            # Valid access but an empty directory: nothing to attest -> skip.
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "Admin SDK Directory returned no users; no org 2SV enforcement state to attest"
            print(json.dumps(result, indent=2))
            return 0

        subtests = _evaluate_2sv(users)
        for name, verdict in subtests.items():
            result["tests"][name] = verdict

        result["interfaces_checked"] = len(result["tests"])
        result["success"] = all(t.get("passed") for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
