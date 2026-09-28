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

"""Create a Firebird service account as the run's access key / IAM user.

Serves control-plane ``create_access_key`` and iam ``create_user``. The account
is an OIDC client of the tenant (``POST /service-accounts``, tenant ADMIN); its client ID and secret are the access key. It is then granted a
project role (default VIEWER, the least-privileged predefined role) on the
run's project so it can reach that project and nothing else.

The account ID is recorded before the role grant, so teardown deletes it even
when the grant fails. The secret is emitted once (the key name is redacted in
isvctl logs) so the next steps can authenticate with it.

Usage:
    python create_service_account.py --name-prefix isv-cp [--project-role VIEWER]

Output JSON:
{
    "success": true,
    "platform": "iam",
    "username": "isv-cp-a1b2c3",
    "user_id": "service-account.xxx",
    "access_key_id": "<client id>",
    "secret_access_key": "<client secret>",
    "project_id": "project.xxx",
    "project_roles": ["VIEWER"]
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import service_accounts
from common.firebird_client import FirebirdClient, log


def main() -> int:
    """Create the service account, grant its project role, and emit JSON.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Create a Firebird service account")
    parser.add_argument("--name-prefix", default="isv-iam", help="Display-name prefix (a random suffix is added)")
    parser.add_argument(
        "--project-role",
        default=service_accounts.MINIMAL_PROJECT_ROLE,
        help="Predefined role granted on the run's project (ADMIN, EDITOR, VIEWER; empty for none)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "iam",
        "username": "",
        "user_id": "",
        "access_key_id": "",
    }
    try:
        client = FirebirdClient()
        account = service_accounts.create(client, args.name_prefix)
        result.update(username=account.name, user_id=account.id, access_key_id=account.client_id)
        result["secret_access_key"] = account.client_secret
        service_accounts.require_credentials(account)

        result["project_id"] = client.project_id
        roles = [args.project_role] if args.project_role else []
        granted = service_accounts.set_project_roles(client, client.project_id, account.id, roles)
        result["project_roles"] = granted
        if sorted(granted) != sorted(roles):
            raise RuntimeError(f"project roles are {granted}, expected {roles}")
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
