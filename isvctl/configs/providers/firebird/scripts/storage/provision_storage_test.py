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

"""Provision a filesystem through the Firebird Filesystem API (HSS01-01).

  api_available     GET /projects/{p}/storage/fs answers (the Filesystem API is served)
  provisioned       POST .../storage/fs; the Operation completes and the readback is READY
  capacity_matches  the readback capacity equals the requested capacity

The filesystem (``isv-fs-<run_id>-hss01``, 1 GiB by default: the platform's
minimum) is deleted on every path once created; a failed delete fails the step
and teardown retries it.

Usage:
    python provision_storage_test.py --run-id a1b2c3 [--capacity-gib 1]

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "provision_storage",
    "filesystem_id": "filesystem.xxx",
    "tests": {
        "api_available":    {"passed": true},
        "provisioned":      {"passed": true},
        "capacity_matches": {"passed": true, "capacity_gib": 1}
    }
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.filesystems import capacity_gib, create_ready, delete_all, fs_name, list_filesystems
from common.firebird_client import FirebirdClient, log, remaining

# Seconds of --timeout reserved for deleting what the step created: the work stops
# this long before the step's deadline, and every delete shares what is left.
CLEANUP_SECONDS = 900
KEYS = ("api_available", "provisioned", "capacity_matches")


def main() -> int:
    """Provision, check, and delete one filesystem.

    Returns:
        0 when every subtest passed and the filesystem was deleted, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird filesystem provisioning test (HSS01-01)")
    parser.add_argument("--run-id", required=True, help="Run ID from the setup step")
    parser.add_argument("--capacity-gib", type=int, default=1, help="Requested capacity in GiB (default 1)")
    parser.add_argument(
        "--timeout", type=int, default=2550, help="Seconds for the whole step, cleanup included (default 2550)"
    )
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "provision_storage", "tests": tests}
    if args.timeout <= CLEANUP_SECONDS:
        parser.error(f"--timeout must exceed the {CLEANUP_SECONDS}s cleanup reserve, got {args.timeout}")
    deadline = time.monotonic() + args.timeout
    work_deadline = deadline - CLEANUP_SECONDS
    client = None
    created: list[str] = []
    try:
        client = FirebirdClient()
        list_filesystems(client)
        tests["api_available"] = {"passed": True}

        fs = create_ready(client, fs_name(args.run_id, "hss01"), args.capacity_gib, remaining(work_deadline), created)
        result["filesystem_id"] = fs.get("id")
        tests["provisioned"] = {"passed": True}

        actual = capacity_gib(fs)
        tests["capacity_matches"] = {"passed": actual == args.capacity_gib, "capacity_gib": actual}
        if actual != args.capacity_gib:
            tests["capacity_matches"]["error"] = f"requested {args.capacity_gib} GiB, read back {actual}"
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")
    finally:
        if client and created:
            cleanup_errors = delete_all(client, created, remaining(deadline))
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors

    for key in KEYS:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values()) and not result.get("cleanup_errors")
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
