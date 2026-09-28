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

"""Release the run's mounts on the BM and delete every filesystem the storage run created.

With a BM configured, it first unmounts every ``/mnt/isv-<run_id>*`` mount
(and removes those directories) and deletes the run's own suite-owned client
token file, ``/root/.weka/isv-<run_id>.json`` - never the tenant's own
``auth-token.json``; the filesystem client stays installed. A filesystem is deleted
only after that, so no delete races a live mount.

Each storage step deletes its own filesystems on every path; this is the net
for the ones a failed delete (or the StorageProviderApiCheck shim, which only
logs a failed ``delete_volume``) left behind. It deletes exactly the
filesystems named with the run's prefix ``isv-fs-<run_id>-``, where ``run_id``
comes from the ``setup`` step, so a concurrent run's filesystems and the
tenant's own are never touched. Without a
run ID (setup did not run) there is nothing to delete.

Usage:
    python teardown.py --run-id a1b2c3 [--instance-id bm.xxx --key-file /tmp/key]

Output JSON:
{
    "success": true,
    "platform": "storage",
    "resources_deleted": ["mount:/mnt/isv-a1b2c3", "token:/root/.weka/isv-a1b2c3.json",
                          "filesystem:filesystem.x (isv-fs-a1b2c3-api)"],
    "resources_failed": [],
    "message": "Deleted 1 filesystem(s) left by run a1b2c3"
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.filesystems import delete_filesystem, list_filesystems, run_prefix
from common.firebird_client import FirebirdClient, log, remaining
from common.probes import running_host
from common.wekafs import bm_configured, redact, release_bm, run_mount_points, token_file


def main() -> int:
    """Delete the run's remaining filesystems.

    Returns:
        0 when none is left, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Delete the Firebird storage run's filesystems")
    parser.add_argument("--run-id", default="", help="Run ID from the setup step (empty: nothing to delete)")
    parser.add_argument("--timeout", type=int, default=1650, help="Seconds for all the deletes together (default 1650)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    args = parser.parse_args()
    # One deadline for every delete, so N leftovers cannot outlast the step timeout.
    deadline = time.monotonic() + args.timeout

    deleted: list[str] = []
    failed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "resources_deleted": deleted,
        "resources_failed": failed,
    }
    if not args.run_id:
        result.update(success=True, message="No run ID (setup did not run); nothing to delete")
        print(json.dumps(result, indent=2))
        return 0

    try:
        prefix = run_prefix(args.run_id)
        client = FirebirdClient()
        if bm_configured(args):
            try:
                host = running_host(client, args.instance_id, args.ssh_user, args.key_file)
                released, release_failed = release_bm(
                    host, run_mount_points(host, args.run_id), token_paths=[token_file(args.run_id)]
                )
            except Exception as e:
                released, release_failed = [], [f"bm:{args.instance_id}: {redact(e)}"]
            deleted.extend(released)
            failed.extend(release_failed)
            for entry in released:
                log(f"  released {entry}")
        for fs in list_filesystems(client):
            name = str(fs.get("name", ""))
            if not name.startswith(prefix):
                continue
            label = f"filesystem:{fs.get('id')} ({name})"
            try:
                delete_filesystem(client, str(fs.get("id", "")), remaining(deadline))
                deleted.append(label)
                log(f"  deleted {label}")
            except Exception as e:
                failed.append(f"{label}: {e}")
                log(f"  could not delete {label}: {e}")
        result["success"] = not failed
        filesystems = sum(entry.startswith("filesystem:") for entry in deleted)
        result["message"] = f"Deleted {filesystems} filesystem(s) left by run {args.run_id}" + (
            f", {len(failed)} failed" if failed else ""
        )
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
