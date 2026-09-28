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

"""Home-directory quota and usage accounting on wekafs (DIR01-01, DIR01-02).

The filesystem-wide quota of a Firebird filesystem is its capacity, set and
changed through the Filesystem API. The step creates its own
``isv-fs-<run_id>-dir`` (``--capacity-gib``, 1 GiB) so filling it cannot
disturb the other checks, mounts it at ``/mnt/isv-<run_id>-dir``, and checks
what a client sees:

  filesystem_quota_configured  the mount reports the requested capacity
  filesystem_quota_updated     after ``PUT`` to ``--to-gib`` (2 GiB) the mount
                               reports the new capacity (polled)
  filesystem_quota_enforced    a write past the reported capacity is refused
                               with ENOSPC

Usage accounting runs on the run's shared mount: files owned by two uids and two
gids are written and the bytes are summed per owner from a fresh walk:

  uid_usage_accounted, gid_usage_accounted  each identity's total is exactly what it wrote
  identity_usage_isolated                   the identities' totals differ as written

The step unmounts and deletes its filesystem on every path, and removes its
files. DIR02-01 (NFSv4) is a separate step. Skips without a BM.

Usage:
    python home_directory_storage_test.py --run-id a1b2c3 --instance-id bm.xxx --key-file /tmp/key \
        --mount-point /mnt/isv-a1b2c3

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "home_directory_storage",
    "tests": {
        "filesystem_quota_configured": {"passed": true, "capacity_gib": 1, "reported_bytes": 1073741824},
        "filesystem_quota_updated":    {"passed": true, "capacity_gib": 2, "reported_bytes": 2147483648},
        "filesystem_quota_enforced":   {"passed": true, "written_bytes": 2147483648},
        "uid_usage_accounted":         {"passed": true},
        "gid_usage_accounted":         {"passed": true},
        "identity_usage_isolated":     {"passed": true}
    }
}
"""

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.filesystems import GIB_BYTES, POLL_SECONDS, create_ready, delete_all, fs_name, submit_resize, wait_ready
from common.firebird_client import FirebirdClient, log, remaining
from common.wekafs import (
    DEFAULT_MOUNT_OPTIONS,
    fill_missing,
    mount_filesystem,
    mount_point,
    mounted_host,
    redact,
    remote,
    run_python,
    statvfs,
    unmount,
)

QUOTA_KEYS = ("filesystem_quota_configured", "filesystem_quota_updated", "filesystem_quota_enforced")
USAGE_KEYS = ("uid_usage_accounted", "gid_usage_accounted", "identity_usage_isolated")
KEYS = (*QUOTA_KEYS, *USAGE_KEYS)
CLEANUP_SECONDS = 900
# A client may report a little more than the requested capacity once data lands.
CAPACITY_TOLERANCE = 0.02
OVERSHOOT_MIB = 64

# Runs as root on the BM: writes files for two uid/gid pairs, sums bytes per owner, removes them.
ACCOUNTING = """
import json, os, shutil, sys
root = os.path.join(sys.argv[1], ".isv-acct-" + os.urandom(4).hex())
owners = {"a": (20001, 30001, 8), "b": (20002, 30002, 4)}
os.mkdir(root)
try:
    for tag, (uid, gid, mib) in owners.items():
        path = os.path.join(root, tag)
        with open(path, "wb") as f:
            f.write(os.urandom(mib << 20))
            f.flush()
            os.fsync(f.fileno())
        os.chown(path, uid, gid)
    by_uid, by_gid = {}, {}
    for entry in os.scandir(root):
        st = os.stat(entry.path)
        by_uid[st.st_uid] = by_uid.get(st.st_uid, 0) + st.st_size
        by_gid[st.st_gid] = by_gid.get(st.st_gid, 0) + st.st_size
finally:
    shutil.rmtree(root, ignore_errors=True)
print(json.dumps({"expected": {tag: [uid, gid, mib << 20] for tag, (uid, gid, mib) in owners.items()},
                  "by_uid": by_uid, "by_gid": by_gid}))
"""


def _within(reported: int, gib: int) -> bool:
    """Return whether ``reported`` bytes is the capacity ``gib`` within the tolerance."""
    return gib * GIB_BYTES <= reported <= gib * GIB_BYTES * (1 + CAPACITY_TOLERANCE)


def accounting(host: Any, path: str) -> dict[str, dict[str, Any]]:
    """Return the DIR01-02 subtests from the per-owner byte totals on ``path``."""
    probe = run_python(host, ACCOUNTING, [path], timeout=300)
    by_uid = {int(k): v for k, v in probe["by_uid"].items()}
    by_gid = {int(k): v for k, v in probe["by_gid"].items()}
    expected = probe["expected"]
    uid_ok = all(by_uid.get(uid) == size for uid, _, size in expected.values())
    gid_ok = all(by_gid.get(gid) == size for _, gid, size in expected.values())
    sizes = [size for _, _, size in expected.values()]
    isolated = uid_ok and gid_ok and len(set(sizes)) == len(sizes)
    tests = {
        "uid_usage_accounted": {"passed": uid_ok, "totals": {str(k): v for k, v in by_uid.items()}},
        "gid_usage_accounted": {"passed": gid_ok, "totals": {str(k): v for k, v in by_gid.items()}},
        "identity_usage_isolated": {"passed": isolated},
    }
    for key, ok in (
        ("uid_usage_accounted", uid_ok),
        ("gid_usage_accounted", gid_ok),
        ("identity_usage_isolated", isolated),
    ):
        if not ok:
            tests[key]["error"] = f"per-owner totals do not match what each owner wrote: {expected}"
    return tests


def wait_capacity(host: Any, path: str, gib: int, timeout: int) -> int:
    """Poll the mount until it reports ``gib`` GiB or ``timeout``; return the last reported bytes."""
    deadline = time.monotonic() + timeout
    while True:
        reported = int(statvfs(host, path)["total_bytes"])
        if _within(reported, gib) or time.monotonic() >= deadline:
            return reported
        time.sleep(POLL_SECONDS)


def main() -> int:
    """Run the quota subtests on a dedicated filesystem and the accounting subtests on the shared mount.

    Returns:
        0 when the probes ran and the filesystem was deleted (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird home-directory storage test (DIR01-01, DIR01-02)")
    parser.add_argument("--run-id", required=True, help="Run ID from the setup step")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    parser.add_argument("--capacity-gib", type=int, default=1, help="Initial filesystem capacity (default 1)")
    parser.add_argument("--to-gib", type=int, default=2, help="Capacity after the quota update (default 2)")
    parser.add_argument("--mount-options", default=DEFAULT_MOUNT_OPTIONS, help="wekafs mount options (default net=udp)")
    parser.add_argument("--settle-timeout", type=int, default=300, help="Seconds for the mount to show a new capacity")
    parser.add_argument(
        "--timeout", type=int, default=3450, help="Seconds for the step, cleanup included (default 3450)"
    )
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "home_directory_storage",
        "tests": tests,
    }
    if args.timeout <= CLEANUP_SECONDS:
        parser.error(f"--timeout must exceed the {CLEANUP_SECONDS}s cleanup reserve, got {args.timeout}")
    host, code = mounted_host(args, result, KEYS)
    if host is None:
        print(json.dumps(result, indent=2))
        return code or 0

    deadline = time.monotonic() + args.timeout
    work_deadline = deadline - CLEANUP_SECONDS
    client = FirebirdClient()
    created: list[str] = []
    path = mount_point(args.run_id, "dir")
    cleanup_errors: list[str] = []
    try:
        tests.update(accounting(host, args.mount_point))

        fs = create_ready(client, fs_name(args.run_id, "dir"), args.capacity_gib, remaining(work_deadline), created)
        mount_filesystem(client, host, fs, path, args.mount_options)
        reported = int(statvfs(host, path)["total_bytes"])
        tests["filesystem_quota_configured"] = {
            "passed": _within(reported, args.capacity_gib),
            "capacity_gib": args.capacity_gib,
            "reported_bytes": reported,
        }
        if not tests["filesystem_quota_configured"]["passed"]:
            tests["filesystem_quota_configured"]["error"] = (
                f"the mount reports {reported} bytes for {args.capacity_gib} GiB"
            )

        client.wait_operation(submit_resize(client, fs, args.to_gib), remaining(work_deadline))
        wait_ready(client, str(fs.get("id", "")), min(args.settle_timeout, remaining(work_deadline)))
        reported = wait_capacity(host, path, args.to_gib, min(args.settle_timeout, remaining(work_deadline)))
        tests["filesystem_quota_updated"] = {
            "passed": _within(reported, args.to_gib),
            "capacity_gib": args.to_gib,
            "reported_bytes": reported,
        }
        if not tests["filesystem_quota_updated"]["passed"]:
            tests["filesystem_quota_updated"]["error"] = (
                f"after the resize to {args.to_gib} GiB the mount still reports {reported} bytes"
            )

        count = reported // (1 << 20) + OVERSHOOT_MIB
        target = shlex.quote(f"{path}/fill")
        code, stdout, stderr = remote(
            host,
            f"findmnt -n -t wekafs -M {shlex.quote(path)} >/dev/null && "
            f"sudo dd if=/dev/zero of={target} bs=1M count={count} oflag=direct status=none; rc=$?; "
            f"stat -c %s {target}; exit $rc",
            timeout=max(60, remaining(work_deadline)),
        )
        written = int(stdout.split()[-1]) if stdout.split() and stdout.split()[-1].isdigit() else 0
        refused = code != 0 and "no space" in stderr.lower()
        tests["filesystem_quota_enforced"] = {"passed": refused, "written_bytes": written, "limit_bytes": reported}
        if refused:
            tests["filesystem_quota_enforced"]["message"] = redact(stderr)
        else:
            tests["filesystem_quota_enforced"]["error"] = (
                f"a {count} MiB write past the {reported}-byte capacity was not refused with ENOSPC: "
                f"{redact(stderr) or f'exit {code}'}"
            )
        remote(host, f"sudo rm -f {target}", timeout=300)
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")
    finally:
        try:
            unmount(host, path)
        except Exception as e:
            cleanup_errors.append(f"mount:{path}: {redact(e)}")
        if created:
            cleanup_errors.extend(delete_all(client, created, remaining(deadline)))
    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors
        result["success"] = False

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
