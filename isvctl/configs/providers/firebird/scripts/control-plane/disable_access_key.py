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

"""Disable a service account's access key by rotating its secret (CP06-01).

Firebird has no "disable key" operation. ``POST /service-accounts/{id}/secret``
regenerates the client secret, which the API documents as invalidating
the previous one - so the old secret is the disabled key. The status is not
assumed from the rotation: the old secret is tried once at ``/auth/token`` and
the key is ``Inactive`` only if it is refused. (An identity provider whose
secret rotation keeps the old secret valid for a grace period shows up here as
``Active``.) The new secret is never emitted.

Usage:
    python disable_access_key.py --service-account-id service-account.xxx \
        --client-id <id> --client-secret=<old secret>

Output JSON:
{
    "success": true,
    "platform": "iam",
    "access_key_id": "<client id>",
    "status": "Inactive",
    "evidence": "old secret refused at /auth/token (HTTP 401)"
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
    """Rotate the secret, then classify the old secret as Active or Inactive.

    Returns:
        0 when the old secret is refused, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Disable a Firebird access key via secret rotation")
    parser.add_argument("--service-account-id", default="", help="Service account (service-account.ULID)")
    parser.add_argument("--client-id", default="", help="Service-account client ID")
    parser.add_argument("--client-secret", default="", help="Secret to disable (the current one)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "iam",
        "access_key_id": args.client_id,
        # Active until the old secret is shown to be refused.
        "status": "Active",
    }
    try:
        if not (args.service_account_id and args.client_id and args.client_secret):
            raise RuntimeError("no service account or credentials (did the create step fail?)")
        client = FirebirdClient()
        service_accounts.rotate_secret(client, args.service_account_id)
        refused, evidence = service_accounts.token_refused(client, args.client_id, args.client_secret)
        if refused:
            result.update(status="Inactive", evidence=f"old secret refused at /auth/token ({evidence})")
            result["success"] = True
        else:
            result["error"] = (
                "secret rotated, but the old secret still issues tokens "
                "(a client-secret rotation policy with a grace period keeps rotated secrets valid)"
            )
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
