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

"""Verify a Firebird bare-metal server was released after teardown.

Independently re-reads the platform state instead of trusting teardown's own
report. With --expect-released (launch provisioned the BM) the BM must no
longer run an OS: AVAILABLE, or gone from the project. The local SSH key pair
must be deleted when --key-file is given.

Usage:
    python verify_terminated.py --instance-id bm.xxx --expect-released [--key-file /tmp/key]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "checks": {
        "instance_terminated": {"passed": true, "message": "..."},
        "key_deleted": {"passed": true}
    }
}
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdApiError, FirebirdClient, log


def main() -> int:
    """Confirm the BM is deprovisioned (or detached) and the key pair is gone.

    Returns:
        0 when every check passes, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Verify Firebird BM released")
    parser.add_argument("--instance-id", default="", help="BM ID (bm.ULID)")
    parser.add_argument("--expect-released", action="store_true", help="The BM must be deprovisioned")
    parser.add_argument("--key-file", help="SSH private key path that must be deleted")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "checks": {},
    }

    # Skip verification when teardown was intentionally skipped (dev workflow)
    if os.environ.get("BM_SKIP_TEARDOWN") == "true":
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "Verification skipped (BM_SKIP_TEARDOWN=true)"
        result["message"] = result["skip_reason"]
        print(json.dumps(result, indent=2))
        return 0

    try:
        if args.expect_released and args.instance_id:
            try:
                state = FirebirdClient().get_bm(args.instance_id).get("state")
                message = f"BM state is {state}"
            except FirebirdApiError as e:
                if e.status != 404:
                    raise
                state = None
                message = "BM is no longer attached to the project"
            result["checks"]["instance_terminated"] = {
                "passed": state in (None, "AVAILABLE", "ALLOCATED"),
                "message": message,
            }

        if args.key_file:
            result["checks"]["key_deleted"] = {"passed": not Path(args.key_file).exists()}

        result["success"] = all(check["passed"] for check in result["checks"].values())
        if not result["success"]:
            failed = [name for name, check in result["checks"].items() if not check["passed"]]
            result["error"] = f"Checks failed: {', '.join(failed)}"

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
