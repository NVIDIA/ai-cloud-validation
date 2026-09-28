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

"""Show a disabled (rotated-out) access key is refused (CP06-01).

Tries the old client secret at ``/auth/token`` until it is refused (HTTP 400/401)
or the attempts run out. An old secret that still issues tokens after every
attempt is reported as not rejected - the result a client-secret rotation
policy with a grace period produces - so the check fails rather than
passing on the rotation call alone.

Usage:
    python verify_key_rejected.py --client-id <id> --client-secret=<old secret> [--retries 5 --wait 5]

Output JSON:
{
    "success": true,
    "platform": "iam",
    "rejected": true,
    "error_code": "HTTP 401",
    "attempts": 1
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import service_accounts
from common.firebird_client import FirebirdClient, log


def main() -> int:
    """Probe the old secret and emit whether it was refused.

    Returns:
        0 when the secret is refused, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Verify a rotated-out Firebird secret is refused")
    parser.add_argument("--client-id", default="", help="Service-account client ID")
    parser.add_argument("--client-secret", default="", help="The rotated-out (old) secret")
    parser.add_argument("--retries", type=int, default=5, help="Attempts while the rotation propagates")
    parser.add_argument("--wait", type=float, default=5, help="Seconds between attempts")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "iam", "rejected": False, "attempts": 0}
    try:
        if not (args.client_id and args.client_secret):
            raise RuntimeError("no credentials to probe (did the create step fail?)")
        client = FirebirdClient()
        for attempt in range(1, max(1, args.retries) + 1):
            result["attempts"] = attempt
            refused, evidence = service_accounts.token_refused(client, args.client_id, args.client_secret)
            if refused:
                result.update(success=True, rejected=True, error_code=evidence)
                break
            if attempt < args.retries:
                time.sleep(args.wait)
        else:
            result["error"] = (
                f"old secret still issues tokens after {result['attempts']} attempt(s); "
                "a client-secret rotation policy keeps rotated secrets valid for its grace period"
            )
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
