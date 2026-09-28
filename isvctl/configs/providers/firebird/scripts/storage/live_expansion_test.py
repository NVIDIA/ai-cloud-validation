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

"""Grow a filesystem's capacity live through the Firebird Filesystem API (HSS10-01).

Creates ``isv-fs-<run_id>-hss10`` at ``--from-gib`` (1 GiB by default), then
``PUT .../storage/fs/{id}`` with ``--to-gib`` (2 GiB) and waits for the Operation:

  capacity_expanded    requires the filesystem readback to show the requested
                       size and ``GET .../storage/fs/status`` to show the
                       expected allocation increase (``backend_allocation_delta_gib``)
                       since BEFORE the create; two agreeing status reads are
                       required to rule out transient values. The baseline
                       predates the create so the create's own allocation, if
                       it lands late, cannot pass for the resize. Another
                       project's create or delete in the same tenant during
                       the step can shift the allocation.
  metadata_consistent  control-plane evidence only: after the resize the
                       filesystem keeps its ID, name, and creation time and reads
                       back READY (``evidence: control_plane``)
  inodes_expanded      with a BM configured (``BM_INSTANCE_ID`` + ``BM_KEY_FILE``;
                       ``setup_mount`` installed the client), the filesystem is
                       mounted at ``/mnt/isv-<run_id>-hss10`` and the inode count
                       the client reports (statvfs ``f_files``) is larger after
                       the resize than before it
  io_uninterrupted     on that mount a background writer writes, fsyncs, and
                       reads back 64 KiB files every 0.1 s from before the resize
                       until after it settles: no I/O error and no gap longer
                       than ``--io-stall-seconds`` between completed operations

Without a BM the two mount subtests fail as not supported (``supported:
false``). The step succeeds when the API-observable subtests pass (and, with a
BM, the mount was cleaned up); HssLiveExpansionCheck needs all four. The
filesystem is unmounted and deleted on every path once created.

Usage:
    python live_expansion_test.py --run-id a1b2c3 [--from-gib 1] [--to-gib 2] \
        [--instance-id bm.xxx --key-file /tmp/key]

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "live_expansion",
    "tests": {
        "capacity_expanded":   {"passed": true, "from_gib": 1, "to_gib": 2, "backend_allocation_delta_gib": 2,
                               "baseline_allocated_gib": 16384},
        "metadata_consistent": {"passed": true, "evidence": "control_plane"},
        "inodes_expanded":     {"passed": true, "files_before": 1000000, "files_after": 2000000},
        "io_uninterrupted":    {"passed": true, "ops": 240, "errors": 0, "max_gap_s": 0.4}
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

from common.filesystems import (
    POLL_SECONDS,
    capacity_gib,
    create_ready,
    delete_all,
    fs_name,
    get_filesystem,
    storage_status,
    submit_resize,
    wait_ready,
)
from common.firebird_client import FirebirdClient, log, remaining
from common.probes import Host, running_host
from common.wekafs import (
    DEFAULT_MOUNT_OPTIONS,
    NO_BM,
    bm_configured,
    mount_filesystem,
    mount_point,
    redact,
    remote,
    statvfs,
    unmount,
)

# Seconds of --timeout reserved for deleting what the step created: the work stops
# this long before the step's deadline, and every delete shares what is left.
CLEANUP_SECONDS = 900
MOUNT_ONLY_REASON = f"not supported: needs a mounted client ({NO_BM})"
MOUNT_ONLY = ("inodes_expanded", "io_uninterrupted")
KEYS = ("capacity_expanded", "metadata_consistent", *MOUNT_ONLY)
# The allocation reported by `/storage/fs/status` can lag a delete by a few
# seconds, so the check polls: the baseline waits for two agreeing reads this
# far apart.
BASELINE_STABLE_SECONDS = 10
BASELINE_TIMEOUT = 120
IO_WARMUP_SECONDS = 5  # the writer runs this long before the resize and after it settles
IO_MAX_SECONDS = 1800  # the writer stops by itself after this, even if never told to

# Runs as root on the BM, detached: argv = mount point, result file, max seconds.
# Writes, fsyncs, and reads back 64 KiB files until the stop file appears.
IO_LOOP = """
import json, os, sys, time
root, out, max_s = sys.argv[1], sys.argv[2], float(sys.argv[3])
stop, work = os.path.join(root, ".isv-io-stop"), os.path.join(root, ".isv-io")
os.makedirs(work, exist_ok=True)
ops = errors = i = 0
first_error, max_gap = "", 0.0
last = time.monotonic()
end = last + max_s
while time.monotonic() < end and not os.path.exists(stop):
    data = os.urandom(65536)
    try:
        path = os.path.join(work, "f%d" % (i % 64))
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with open(path, "rb") as f:
            if f.read() != data:
                raise IOError("read back different data")
        ops += 1
        now = time.monotonic()
        max_gap, last = max(max_gap, now - last), now
    except Exception as e:
        errors += 1
        first_error = first_error or "%s: %s" % (type(e).__name__, e)
    i += 1
    time.sleep(0.1)
with open(out + ".tmp", "w") as f:
    json.dump({"ops": ops, "errors": errors, "max_gap_s": round(max(max_gap, time.monotonic() - last), 2),
               "first_error": first_error}, f)
os.replace(out + ".tmp", out)
"""


class LiveIo:
    """The mount and background writer that watch a filesystem through its resize."""

    def __init__(self, host: Host, path: str, run_id: str) -> None:
        """Remember where the filesystem is mounted; the writer's paths are set once staged."""
        self.host = host
        self.path = path
        self.run_id = run_id
        self.workdir = ""
        self.script = ""
        self.out = ""
        self.files_before = 0
        self.started = False

    def start(self) -> None:
        """Record the inode count and start the writer detached on the BM.

        The writer script and its output/temp files are staged under a fresh
        ``mktemp -d`` directory made root-only (0700) under ``/root``, never a
        predictable world-writable ``/tmp`` path.
        """
        self.files_before = int(statvfs(self.host, self.path)["files"])
        code, stdout, stderr = remote(self.host, "sudo mktemp -d /root/isv-hss10-io.XXXXXX")
        if code != 0 or not stdout.strip():
            raise RuntimeError(f"could not stage the I/O writer directory: {redact(stderr or stdout)}")
        self.workdir = stdout.strip()
        self.script = f"{self.workdir}/io.py"
        self.out = f"{self.workdir}/io.json"
        self.started = True  # from here on, cleanup removes the staging directory
        code, stdout, stderr = remote(self.host, f"sudo tee {shlex.quote(self.script)} >/dev/null", input_text=IO_LOOP)
        if code != 0:
            raise RuntimeError(f"could not stage the I/O writer: {redact(stderr or stdout)}")
        args = " ".join(shlex.quote(a) for a in (self.script, self.path, self.out, str(IO_MAX_SECONDS)))
        code, stdout, stderr = remote(
            self.host,
            f"sudo rm -f {shlex.quote(self.out)}; sudo setsid nohup python3 {args} </dev/null >/dev/null 2>&1 &",
        )
        if code != 0:
            raise RuntimeError(f"could not start the I/O writer: {redact(stderr or stdout)}")
        time.sleep(IO_WARMUP_SECONDS)

    def finish(self, stall_seconds: float, timeout: int = 120) -> dict[str, dict[str, Any]]:
        """Stop the writer and return the ``inodes_expanded`` and ``io_uninterrupted`` subtests."""
        time.sleep(IO_WARMUP_SECONDS)
        files_after = int(statvfs(self.host, self.path)["files"])
        remote(self.host, f"sudo touch {shlex.quote(self.path + '/.isv-io-stop')}")
        deadline = time.monotonic() + timeout
        while True:
            code, stdout, _ = remote(self.host, f"sudo cat {shlex.quote(self.out)} 2>/dev/null")
            if code == 0 and stdout.strip():
                io = json.loads(stdout)
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"the I/O writer did not report within {timeout}s")
            time.sleep(POLL_SECONDS)
        inodes: dict[str, Any] = {
            "passed": files_after > self.files_before,
            "files_before": self.files_before,
            "files_after": files_after,
        }
        if not inodes["passed"]:
            inodes["error"] = f"the mount reports {files_after} inodes after the resize, {self.files_before} before"
        uninterrupted: dict[str, Any] = {
            "passed": io["ops"] > 0 and io["errors"] == 0 and io["max_gap_s"] <= stall_seconds,
            "ops": io["ops"],
            "errors": io["errors"],
            "max_gap_s": io["max_gap_s"],
        }
        if io["errors"]:
            uninterrupted["error"] = (
                f"{io['errors']} I/O error(s) during the resize, first: {redact(io['first_error'])}"
            )
        elif not io["ops"]:
            uninterrupted["error"] = "the writer completed no operation"
        elif io["max_gap_s"] > stall_seconds:
            uninterrupted["error"] = f"I/O stalled for {io['max_gap_s']}s (limit {stall_seconds:g}s)"
        return {"inodes_expanded": inodes, "io_uninterrupted": uninterrupted}

    def cleanup(self) -> None:
        """Stop the writer, remove its staging directory and mount-side files, and unmount."""
        if self.started:
            remote(
                self.host,
                f"sudo touch {shlex.quote(self.path + '/.isv-io-stop')} 2>/dev/null; sleep 1; "
                f"sudo rm -rf {shlex.quote(self.path + '/.isv-io')} {shlex.quote(self.path + '/.isv-io-stop')} "
                f"{shlex.quote(self.workdir)}",
                timeout=120,
            )
        unmount(self.host, self.path)


def settled_allocation(client: FirebirdClient, timeout: int) -> int:
    """Return the organization allocation once two reads ``BASELINE_STABLE_SECONDS`` apart agree.

    A release still pending from an earlier step's delete would otherwise sit in
    the baseline and hide growth. Returns the last read at ``timeout``.
    """
    deadline = time.monotonic() + timeout
    last = storage_status(client)[1]
    while time.monotonic() < deadline:
        time.sleep(BASELINE_STABLE_SECONDS)
        current = storage_status(client)[1]
        if current == last:
            return current
        last = current
    return last


def wait_allocation(client: FirebirdClient, baseline: int, growth: int, timeout: int) -> int:
    """Poll the organization allocation until it grew by ``growth`` GiB or ``timeout``; return the growth seen."""
    deadline = time.monotonic() + timeout
    while True:
        delta = storage_status(client)[1] - baseline
        if delta >= growth or time.monotonic() >= deadline:
            return delta
        time.sleep(POLL_SECONDS)


def main() -> int:
    """Create, grow, check, and delete one filesystem.

    Returns:
        0 when the API-observable subtests passed and the filesystem was deleted, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird live filesystem expansion test (HSS10-01)")
    parser.add_argument("--run-id", required=True, help="Run ID from the setup step")
    parser.add_argument("--from-gib", type=int, default=1, help="Initial capacity in GiB (default 1)")
    parser.add_argument("--to-gib", type=int, default=2, help="Capacity after the resize in GiB (default 2)")
    parser.add_argument(
        "--settle-timeout",
        type=int,
        default=300,
        help="Seconds to wait after the resize for a READY readback and the expected allocation",
    )
    parser.add_argument(
        "--timeout", type=int, default=3450, help="Seconds for the whole step, cleanup included (default 3450)"
    )
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    parser.add_argument("--mount-options", default=DEFAULT_MOUNT_OPTIONS, help="wekafs mount options (default net=udp)")
    parser.add_argument(
        "--io-stall-seconds", type=float, default=30, help="Longest I/O gap allowed during the resize (default 30)"
    )
    args = parser.parse_args()

    with_bm = bm_configured(args)
    tests: dict[str, dict[str, Any]] = (
        {}
        if with_bm
        else {key: {"passed": False, "supported": False, "error": MOUNT_ONLY_REASON} for key in MOUNT_ONLY}
    )
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "live_expansion", "tests": tests}
    if args.timeout <= CLEANUP_SECONDS:
        parser.error(f"--timeout must exceed the {CLEANUP_SECONDS}s cleanup reserve, got {args.timeout}")
    deadline = time.monotonic() + args.timeout
    work_deadline = deadline - CLEANUP_SECONDS
    client = None
    created: list[str] = []
    live: LiveIo | None = None
    try:
        if args.to_gib <= args.from_gib:
            raise ValueError(f"--to-gib ({args.to_gib}) must exceed --from-gib ({args.from_gib})")
        client = FirebirdClient()
        # The baseline is read before the create: the create's own allocation can
        # land late, and measured from after the create it would pass for the resize.
        baseline = settled_allocation(client, min(BASELINE_TIMEOUT, remaining(work_deadline)))
        before = create_ready(client, fs_name(args.run_id, "hss10"), args.from_gib, remaining(work_deadline), created)
        fs_id = str(before.get("id", ""))
        if with_bm:
            try:
                host = running_host(client, args.instance_id, args.ssh_user, args.key_file)
                path = mount_point(args.run_id, "hss10")
                live = LiveIo(host, path, args.run_id)
                mount_filesystem(client, host, before, path, args.mount_options)
                live.start()
            except Exception as e:
                error = f"could not watch the resize from a mount: {redact(e)}"
                log(f"  {error}")
                tests.update({key: {"passed": False, "error": error} for key in MOUNT_ONLY})

        client.wait_operation(submit_resize(client, before, args.to_gib), remaining(work_deadline))
        settle_deadline = time.monotonic() + min(args.settle_timeout, remaining(work_deadline))
        try:
            after = wait_ready(client, fs_id, remaining(settle_deadline))
        except RuntimeError as e:
            log(f"  {e}")
            after = get_filesystem(client, fs_id) or {}
        growth = args.to_gib
        delta = wait_allocation(client, baseline, growth, remaining(settle_deadline))

        readback = capacity_gib(after)
        expanded: dict[str, Any] = {
            "passed": readback == args.to_gib and delta >= growth,
            "from_gib": capacity_gib(before),
            "to_gib": readback,
            "backend_allocation_delta_gib": delta,
            "baseline_allocated_gib": baseline,
        }
        if readback != args.to_gib:
            expanded["error"] = f"requested {args.to_gib} GiB, read back {readback}"
        elif delta < growth:
            expanded["error"] = (
                f"the API reads back {readback} GiB but the Weka organization's allocation grew by {delta} GiB "
                f"since before the create (expected at least {growth}): the resize is not reflected in /storage/fs/status"
            )
        tests["capacity_expanded"] = expanded

        unchanged = [key for key in ("id", "name", "createdAt") if after.get(key) != before.get(key)]
        consistent: dict[str, Any] = {
            "passed": not unchanged and after.get("state") == "READY",
            "evidence": "control_plane",
        }
        if unchanged:
            consistent["error"] = f"changed across the resize: {', '.join(unchanged)}"
        elif after.get("state") != "READY":
            consistent["error"] = f"reads back {after.get('state')} after the resize, expected READY"
        tests["metadata_consistent"] = consistent

        if live and not tests.get("io_uninterrupted"):
            try:
                tests.update(live.finish(args.io_stall_seconds))
            except Exception as e:
                error = redact(e)
                log(f"  {error}")
                tests.update({key: {"passed": False, "error": error} for key in MOUNT_ONLY})
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")
    finally:
        if live:
            try:
                live.cleanup()
            except Exception as e:
                result.setdefault("cleanup_errors", []).append(f"mount:{live.path}: {redact(e)}")
        if client and created:
            cleanup_errors = delete_all(client, created, remaining(deadline))
            if cleanup_errors:
                result.setdefault("cleanup_errors", []).extend(cleanup_errors)

    for key in KEYS:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    # The step succeeds when what the API can show passed; HssLiveExpansionCheck
    # still fails on the mount-only subtests.
    result["success"] = all(tests[key]["passed"] for key in KEYS if key not in MOUNT_ONLY) and not result.get(
        "cleanup_errors"
    )
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
