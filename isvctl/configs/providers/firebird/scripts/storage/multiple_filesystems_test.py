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

"""Several filesystems side by side within the tenant's storage quota (HSS09-01).

Creates ``--count`` filesystems (``isv-fs-<run_id>-hss09-<n>``, 1 GiB each by
default), then:

  multiple_filesystems   every one is READY and listed in the same
                         GET /projects/{p}/storage/fs (``filesystem_count``)
  within_total_capacity  GET .../storage/fs/status shows the tenant's organization
                         allocation (every filesystem in the tenant, these
                         included) within the total quota
  min_fs_size            the smallest one, in TiB (``min_size_tib``), is at most
                         ``--max-fs-tib`` (the suite's 50 TiB ceiling); it shows
                         the platform provisions filesystems that small

Every filesystem is deleted on every path once created; a failed delete fails
the step and teardown retries it.

Usage:
    python multiple_filesystems_test.py --run-id a1b2c3 [--count 2] [--capacity-gib 1] [--max-fs-tib 50]

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "multiple_filesystems",
    "tests": {
        "multiple_filesystems":  {"passed": true, "filesystem_count": 2},
        "within_total_capacity": {"passed": true, "total_quota_gib": 93132, "allocated_gib": 16386},
        "min_fs_size":           {"passed": true, "min_size_tib": 0.001}
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

from common.filesystems import (
    GIB_PER_TIB,
    capacity_gib,
    create_ready,
    delete_all,
    fs_name,
    list_filesystems,
    storage_status,
)
from common.firebird_client import FirebirdClient, log, remaining

# Seconds of --timeout reserved for deleting what the step created: the work stops
# this long before the step's deadline, and every delete shares what is left.
CLEANUP_SECONDS = 900
KEYS = ("multiple_filesystems", "within_total_capacity", "min_fs_size")


def main() -> int:
    """Create several filesystems, check them together, and delete them.

    Returns:
        0 when every subtest passed and every filesystem was deleted, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird multiple-filesystems test (HSS09-01)")
    parser.add_argument("--run-id", required=True, help="Run ID from the setup step")
    parser.add_argument("--count", type=int, default=2, help="Filesystems to create (at least 2)")
    parser.add_argument("--capacity-gib", type=int, default=1, help="Capacity of each in GiB (default 1)")
    parser.add_argument("--max-fs-tib", type=float, default=50.0, help="Ceiling for the smallest filesystem (TiB)")
    parser.add_argument(
        "--timeout", type=int, default=3450, help="Seconds for the whole step, cleanup included (default 3450)"
    )
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "multiple_filesystems",
        "tests": tests,
    }
    if args.timeout <= CLEANUP_SECONDS:
        parser.error(f"--timeout must exceed the {CLEANUP_SECONDS}s cleanup reserve, got {args.timeout}")
    deadline = time.monotonic() + args.timeout
    work_deadline = deadline - CLEANUP_SECONDS
    client = None
    created: list[str] = []
    try:
        if args.count < 2:
            raise ValueError(f"--count must be at least 2, got {args.count}")
        client = FirebirdClient()
        filesystems = [
            create_ready(
                client, fs_name(args.run_id, f"hss09-{n}"), args.capacity_gib, remaining(work_deadline), created
            )
            for n in range(1, args.count + 1)
        ]

        listed = {fs.get("id"): fs.get("state") for fs in list_filesystems(client)}
        ready = [fs_id for fs_id in created if listed.get(fs_id) == "READY"]
        tests["multiple_filesystems"] = {"passed": len(ready) == args.count, "filesystem_count": len(ready)}
        if len(ready) != args.count:
            tests["multiple_filesystems"]["error"] = f"{len(ready)} of {args.count} listed READY together"

        total_gib, allocated_gib = storage_status(client)
        fits = 0 < total_gib and allocated_gib <= total_gib
        tests["within_total_capacity"] = {
            "passed": fits,
            "total_quota_gib": total_gib,
            "allocated_gib": allocated_gib,
        }
        if not fits:
            tests["within_total_capacity"]["error"] = f"allocated {allocated_gib} GiB of a {total_gib} GiB quota"

        capacities = [capacity_gib(fs) for fs in filesystems]
        if any(c is None for c in capacities):
            tests["min_fs_size"] = {"passed": False, "error": "a filesystem read back no capacity"}
        else:
            min_tib = round(min(c for c in capacities if c is not None) / GIB_PER_TIB, 4)
            tests["min_fs_size"] = {"passed": min_tib <= args.max_fs_tib, "min_size_tib": min_tib}
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
