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

"""Out-of-cluster service-account authentication (SEC03-01).

Creates a service account (``POST /service-accounts``), exchanges its client ID
and secret for a token at ``/auth/token`` (client_credentials), and shows the
API resolves the token to that account (``GET /users/me``). The secret is a
long-lived key; the token it yields is short-lived (``expires_at``). The account
holds no roles and is deleted by the security teardown step.

Usage:
    python sa_credential_test.py

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "sa_credential_test",
    "authenticated": true,
    "credential_type": "oauth2_client_credentials",
    "credential_source": "long_lived_key",
    "identity": "service-account.xxx",
    "expires_at": "2026-09-23T12:05:00Z",
    "created_service_account_ids": ["service-account.xxx"]
}
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import service_accounts
from common.firebird_client import FirebirdClient, log, rfc3339


def main() -> int:
    """Create a service account, authenticate as it, and emit JSON.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Firebird service-account credential test")
    parser.add_argument("--retries", type=int, default=5, help="Token attempts while a new client propagates")
    parser.add_argument("--wait", type=float, default=5, help="Seconds between token attempts")
    args = parser.parse_args()

    created: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "sa_credential_test",
        "authenticated": False,
        "created_service_account_ids": created,
    }
    try:
        account = service_accounts.create(FirebirdClient(), "isv-sec-sa")
        created.append(account.id)
        service_accounts.require_credentials(account)

        sa_client, token = service_accounts.login(account.client_id, account.client_secret, args.retries, args.wait)
        identity = sa_client.request("GET", "/users/me").get("userId") or ""
        result.update(
            credential_type="oauth2_client_credentials",
            credential_source="long_lived_key",
            identity=identity,
            expires_at=rfc3339(datetime.now(UTC) + timedelta(seconds=int(token.get("expiresIn") or 0))),
        )
        if identity != account.id:
            raise RuntimeError(f"token resolves to {identity or 'nobody'}, expected {account.id}")
        result["authenticated"] = True
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
