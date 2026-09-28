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

"""Delete filesystems left behind by earlier, killed storage runs.

A storage step killed mid-way (orchestrator timeout, Ctrl-C) cannot delete what
it created, and a later run has a different run ID, so its teardown does not
see them. This sweep, the storage config's second setup step, finds them by
name instead: filesystems named ``isv-fs-<run id>-<role>`` (every name this
provider creates) that are older than ``--min-age-hours``
(``FIREBIRD_SWEEP_MIN_AGE_HOURS``, default 6), so a concurrent run's
filesystems are never touched. A filesystem without a creation time, and every
other name (the tenant's own filesystems), is left alone; the delete itself
refuses any name not starting with ``isv-fs-``. The sweep skips when it cannot
list filesystems, reports failed deletes, and never fails setup.

With a BM configured it first unmounts stale wekafs mounts under
``/mnt/isv-*``: those of an ``isv-fs-*`` filesystem it is about to delete or
that no longer exists. A younger run's mount is left alone. It also removes
every leftover suite-owned client token (``/root/.weka/isv-*.json``) whose run is
not still mounted - never the tenant's own ``auth-token.json``, which this
provider never writes to or deletes.

Usage:
    python sweep_leftovers.py [--min-age-hours 6] [--instance-id bm.xxx --key-file /tmp/key]

Output JSON:
{
    "success": true,
    "platform": "storage",
    "min_age_hours": 6.0,
    "resources_deleted": ["mount:/mnt/isv-a1b2c3", "filesystem:filesystem.x (isv-fs-a1b2c3-hss01)"],
    "resources_failed": [],
    "message": "Deleted 1 leftover filesystem(s)"
}
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.filesystems import RUN_NAME, delete_filesystem, is_owned, list_filesystems
from common.firebird_client import FirebirdClient, log, parse_timestamp, remaining
from common.probes import running_host
from common.wekafs import (
    bm_configured,
    is_run_mount_point,
    leftover_token_files,
    redact,
    release_bm,
    run_id_of,
    token_run_id,
    wekafs_mounts,
)

DEFAULT_MIN_AGE_HOURS = 6.0


def sweep_mounts(client: FirebirdClient, args: argparse.Namespace, live: set[str]) -> tuple[list[str], list[str]]:
    """Unmount the BM's stale ``/mnt/isv-*`` wekafs mounts; return (released, failed).

    A mount is stale when its filesystem is one of this provider's and is not in
    ``live`` (the filesystems that exist and are younger than the sweep age).
    Every leftover suite-owned token (``/root/.weka/isv-*.json``) whose run ID
    is not still mounted is removed too; the tenant's own token is never touched
    - ``release_bm``/``remove_token`` only ever act on ``isv-*.json`` paths.
    """
    try:
        host = running_host(client, args.instance_id, args.ssh_user, args.key_file)
        mounts = wekafs_mounts(host)
        stale = [
            m["target"]
            for m in mounts
            if is_run_mount_point(m["target"]) and is_owned(name := m["source"].partition("/")[2]) and name not in live
        ]
        if not stale and mounts:
            return [], []
        remaining_mounts = [m for m in mounts if m["target"] not in stale]
        mounted_run_ids = {rid for m in remaining_mounts if (rid := run_id_of(m["target"]))}
        token_paths = [token for token in leftover_token_files(host) if token_run_id(token) not in mounted_run_ids]
        released, failed = release_bm(host, stale, token_paths=token_paths)
        for entry in released:
            log(f"  swept {entry}")
        return released, failed
    except Exception as e:
        return [], [f"bm:{args.instance_id}: {redact(e)}"]


def main() -> int:
    """List, filter, and delete stale run filesystems; always succeed so setup continues.

    Returns:
        0
    """
    parser = argparse.ArgumentParser(description="Delete Firebird isv-fs-* leftovers from killed runs")
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=float(os.environ.get("FIREBIRD_SWEEP_MIN_AGE_HOURS", "").strip() or DEFAULT_MIN_AGE_HOURS),
        help="Only delete filesystems older than this (default 6, FIREBIRD_SWEEP_MIN_AGE_HOURS)",
    )
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
        "success": True,
        "platform": "storage",
        "min_age_hours": args.min_age_hours,
        "resources_deleted": deleted,
        "resources_failed": failed,
    }
    try:
        client = FirebirdClient()
        cutoff = datetime.now(UTC) - timedelta(hours=args.min_age_hours)
        filesystems = list_filesystems(client)
    except Exception as e:
        result.update(skipped=True, skip_reason=f"Leftover sweep skipped: filesystems not listed: {e}")
        print(json.dumps(result, indent=2))
        return 0

    stale = [
        fs
        for fs in filesystems
        if RUN_NAME.match(str(fs.get("name", "")))
        and (created := parse_timestamp(fs.get("createdAt"))) is not None
        and created < cutoff
    ]
    if bm_configured(args):
        live = {str(fs.get("name", "")) for fs in filesystems} - {str(fs.get("name", "")) for fs in stale}
        released, release_failed = sweep_mounts(client, args, live)
        deleted.extend(released)
        failed.extend(release_failed)

    for fs in stale:
        name = str(fs.get("name", ""))
        label = f"filesystem:{fs.get('id')} ({name})"
        try:
            delete_filesystem(client, str(fs.get("id", "")), remaining(deadline))
            deleted.append(label)
            log(f"  swept {label}")
        except Exception as e:
            failed.append(f"{label}: {e}")
            log(f"  could not sweep {label}: {e}")

    filesystems_deleted = sum(entry.startswith("filesystem:") for entry in deleted)
    result["message"] = f"Deleted {filesystems_deleted} leftover filesystem(s)" + (
        f", {len(failed)} failed" if failed else ""
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
