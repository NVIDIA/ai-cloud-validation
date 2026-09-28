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

"""Authenticate as a created service account and reach its project.

Serves control-plane ``test_access_key`` and iam ``test_credentials``:
  identity  ``POST /auth/token`` (client_credentials) issues a token, and
            ``GET /users/me`` with it names the service account
  access    ``GET /projects/{p}`` with it succeeds - the project the account
            was granted a role on

Usage:
    python test_credentials.py --service-account-id service-account.xxx \
        --client-id <id> --client-secret=<secret>

Output JSON:
{
    "success": true,
    "platform": "iam",
    "authenticated": true,
    "identity_id": "service-account.xxx",
    "account_id": "tenant.xxx",
    "tests": {"identity": {"passed": true}, "access": {"passed": true}}
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import service_accounts
from common.firebird_client import log


def main() -> int:
    """Log in with the service-account credentials and probe identity and access.

    Returns:
        0 when both probes pass, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Test Firebird service-account credentials")
    parser.add_argument("--service-account-id", default="", help="Expected identity (service-account.ULID)")
    parser.add_argument("--client-id", default="", help="Service-account client ID")
    parser.add_argument("--client-secret", default="", help="Service-account client secret")
    parser.add_argument("--retries", type=int, default=5, help="Token attempts while a new client propagates")
    parser.add_argument("--wait", type=float, default=5, help="Seconds between token attempts")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "iam", "authenticated": False, "tests": tests}
    try:
        if not args.client_id or not args.client_secret:
            raise RuntimeError("no service-account credentials (did the create step fail?)")
        sa_client, _ = service_accounts.login(args.client_id, args.client_secret, args.retries, args.wait)
        me = sa_client.request("GET", "/users/me")
        identity = me.get("userId") or ""
        result["identity_id"] = identity
        tests["identity"] = {"passed": bool(identity), "message": f"token issued; /users/me is {identity}"}
        if args.service_account_id and identity != args.service_account_id:
            tests["identity"] = {
                "passed": False,
                "error": f"/users/me is {identity or 'empty'}, expected {args.service_account_id}",
            }
        result["authenticated"] = tests["identity"]["passed"]

        project = sa_client.get_project()
        result["account_id"] = project["tenantId"]
        tests["access"] = {"passed": project.get("id") == sa_client.project_id, "message": f"read {project.get('id')}"}
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    for key in ("identity", "access"):
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
