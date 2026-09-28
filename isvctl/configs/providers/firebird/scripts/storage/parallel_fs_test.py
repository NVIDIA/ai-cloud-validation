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

"""Parallel filesystem provisioned through the API and mounted on the BM (HSS07-01).

Checks the filesystem ``setup_mount`` created and mounted:

  api_available           ``GET .../storage/fs/{id}`` answers for it
  filesystem_provisioned  it reads back READY with a capacity (``fs_type``: wekafs)
  mount_successful        it is mounted over wekafs at ``--mount-point`` and a probe
                          file writes, reads back, and is removed there
                          (``mount_point``, ``client_version``, ``mounted``)

Skips without a BM. When ``setup_mount`` failed, ``mount_successful`` fails with
its error, and the API subtests still report on the filesystem if it was created.

Usage:
    python parallel_fs_test.py --instance-id bm.xxx --key-file /tmp/key --mount-point /mnt/isv-a1b2c3 \
        --fs-id filesystem.xxx --client-version 5.1.0

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "provision_parallel_fs",
    "tests": {
        "api_available":          {"passed": true},
        "filesystem_provisioned": {"passed": true, "fs_type": "wekafs", "capacity_gib": 1},
        "mount_successful":       {"passed": true, "mount_point": "/mnt/isv-a1b2c3", "client_version": "5.1.0",
                                   "mounted": true}
    }
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.filesystems import capacity_gib, get_filesystem
from common.firebird_client import FirebirdClient, log
from common.probes import running_host, skipped
from common.wekafs import (
    FS_TYPE,
    NO_BM,
    NOT_MOUNTED,
    bm_configured,
    fill_missing,
    is_run_mount_point,
    mount_of,
    probe_read_write,
    redact,
)

KEYS = ("api_available", "filesystem_provisioned", "mount_successful")


def main() -> int:
    """Check the API readback and the mount of the run's filesystem.

    Returns:
        0 when every subtest ran (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird parallel filesystem provisioning test (HSS07-01)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    parser.add_argument("--fs-id", default="", help="Filesystem setup_mount created")
    parser.add_argument("--client-version", default="", help="Filesystem client version setup_mount reported")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "provision_parallel_fs",
        "tests": tests,
    }
    if not bm_configured(args):
        print(json.dumps(skipped(result, NO_BM), indent=2))
        return 0

    try:
        client = FirebirdClient()
        fs = None
        if args.fs_id:
            fs = get_filesystem(client, args.fs_id)
            tests["api_available"] = {"passed": fs is not None}
            if fs is None:
                tests["api_available"]["error"] = f"GET filesystem {args.fs_id} returned 404"
        else:
            tests["api_available"] = {"passed": False, "error": "setup_mount created no filesystem"}
        if fs is not None:
            ready = fs.get("state") == "READY"
            tests["filesystem_provisioned"] = {"passed": ready, "fs_type": FS_TYPE, "capacity_gib": capacity_gib(fs)}
            if not ready:
                tests["filesystem_provisioned"]["error"] = f"filesystem is {fs.get('state')}, expected READY"

        mounted: dict[str, Any] = {
            "passed": False,
            "mount_point": args.mount_point,
            "client_version": args.client_version,
        }
        if not args.mount_point:
            mounted["error"] = f"{NOT_MOUNTED}: {redact(args.mount_error) or 'setup_mount did not mount it'}"
        elif not is_run_mount_point(args.mount_point):
            mounted["error"] = f"refusing to probe {args.mount_point!r}: not a /mnt/isv-<run_id> mount point"
        else:
            host = running_host(client, args.instance_id, args.ssh_user, args.key_file)
            entry = mount_of(host, args.mount_point)
            name = str((fs or {}).get("name", ""))
            if entry is None:
                mounted["error"] = f"{NOT_MOUNTED}: nothing is mounted at {args.mount_point}"
            elif name and not entry["source"].endswith(f"/{name}"):
                mounted["error"] = f"{args.mount_point} has {entry['source']} mounted, expected {name}"
            else:
                probe = probe_read_write(host, args.mount_point)
                mounted.update(passed=bool(probe.get("ok")), mounted=True, mount_options=entry["options"])
                if not probe.get("ok"):
                    mounted["error"] = "a probe file did not read back what was written"
        tests["mount_successful"] = mounted
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
