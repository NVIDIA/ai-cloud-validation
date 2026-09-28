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

"""Measure bandwidth and IOPS on the mounted filesystem against the QoS minimums (HSS02-01).

Runs on the BM, in the run's wekafs mount, with only python3 (no fio needed):

  bandwidth_meets_min  ``--jobs`` parallel writers each write ``--file-mib`` MiB
                       with O_DIRECT, then read it back with O_DIRECT;
                       ``measured_mbps`` is the lower of the two rates (MB/s)
                       and must reach ``--min-mbps``
  iops_meets_min       ``--iops-jobs`` processes issue random 4 KiB O_DIRECT
                       reads over those files for ``--duration`` seconds;
                       ``measured_iops`` must reach ``--min-iops``

The Filesystem API has no QoS class to request, so the minimums are the suite
reference floor (1000 MB/s, 50000 IOPS by default). The filesystem's own
throughput/IOPS limits (``max_throughput`` / ``max_iops``, 0 = unlimited) are
reported alongside. The test files are removed on every path. Skips without a BM.

Usage:
    python qos_throughput_test.py --instance-id bm.xxx --key-file /tmp/key --mount-point /mnt/isv-a1b2c3

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "qos_throughput",
    "tests": {
        "bandwidth_meets_min": {"passed": true, "measured_mbps": 1200, "min_mbps": 1000,
                                "write_mbps": 1300, "read_mbps": 1200},
        "iops_meets_min":      {"passed": true, "measured_iops": 60000, "min_iops": 50000}
    }
}
"""

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import log
from common.wekafs import fill_missing, mount_of, mounted_host, redact, run_id_of, run_python, token_file, weka_json

KEYS = ("bandwidth_meets_min", "iops_meets_min")

# Runs as root on the BM: argv = mount point, jobs, MiB per job, IOPS processes, seconds.
BENCH = """
import json, mmap, os, random, shutil, sys, time
root, jobs, mib, iops_jobs, duration = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
work = os.path.join(root, ".isv-qos-" + os.urandom(4).hex())
os.mkdir(work)
MIB = 1 << 20

def parallel(count, fn):
    start = time.monotonic()
    pipes = []
    for i in range(count):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                value = fn(i)
                os.write(w, json.dumps({"ok": True, "value": value}).encode())
            except Exception as e:
                os.write(w, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}).encode())
            os._exit(0)
        os.close(w)
        pipes.append((pid, r))
    results = []
    for pid, r in pipes:
        with os.fdopen(r) as f:
            results.append(json.loads(f.read() or '{"ok": false, "error": "worker died"}'))
        os.waitpid(pid, 0)
    errors = [x["error"] for x in results if not x["ok"]]
    if errors:
        raise RuntimeError(errors[0])
    return time.monotonic() - start, [x["value"] for x in results]

def write(i):
    buf = mmap.mmap(-1, MIB)
    buf.write(os.urandom(MIB))
    fd = os.open(os.path.join(work, f"f{i}"), os.O_WRONLY | os.O_CREAT | os.O_DIRECT, 0o600)
    try:
        for _ in range(mib):
            os.write(fd, buf)
        os.fsync(fd)
    finally:
        os.close(fd)
    return mib * MIB

def read(i):
    buf = mmap.mmap(-1, MIB)
    fd = os.open(os.path.join(work, f"f{i}"), os.O_RDONLY | os.O_DIRECT)
    total = 0
    try:
        while (n := os.readv(fd, [buf])) > 0:
            total += n
    finally:
        os.close(fd)
    return total

def random_reads(i):
    buf = mmap.mmap(-1, 4096)
    rnd = random.Random(i)
    fd = os.open(os.path.join(work, f"f{i % jobs}"), os.O_RDONLY | os.O_DIRECT)
    blocks, count = mib * 256, 0
    stop = time.monotonic() + duration
    try:
        while time.monotonic() < stop:
            os.preadv(fd, [buf], rnd.randrange(blocks) * 4096)
            count += 1
    finally:
        os.close(fd)
    return count

try:
    write_s, written = parallel(jobs, write)
    read_s, read_back = parallel(jobs, read)
    iops_s, ops = parallel(iops_jobs, random_reads)
finally:
    shutil.rmtree(work, ignore_errors=True)
print(json.dumps({"write_mbps": sum(written) / write_s / 1e6, "read_mbps": sum(read_back) / read_s / 1e6,
                  "bytes": sum(written), "iops": sum(ops) / max(iops_s, duration)}))
"""


def fs_limits(host: Any, source: str, token_path: str) -> str:
    """Return the filesystem's QoS limits as text ("" when unreadable)."""
    backend, _, name = source.partition("/")
    try:
        fs = next(
            (f for f in weka_json(host, f"fs -H {shlex.quote(backend)}", token_path) if f.get("name") == name), None
        )
    except RuntimeError as e:
        log(f"  Weka limits not readable: {e}")
        return ""
    if fs is None:
        return ""
    return f"Weka limits max_throughput={fs.get('max_throughput')} max_iops={fs.get('max_iops')} (0 = unlimited)"


def main() -> int:
    """Run the bandwidth and IOPS benchmarks on the mount.

    Returns:
        0 when both benchmarks ran (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird QoS throughput test (HSS02-01)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    parser.add_argument("--min-mbps", type=float, default=1000, help="Minimum bandwidth in MB/s (default 1000)")
    parser.add_argument("--min-iops", type=float, default=50000, help="Minimum 4 KiB IOPS (default 50000)")
    parser.add_argument("--jobs", type=int, default=4, help="Parallel sequential writers/readers (default 4)")
    parser.add_argument("--file-mib", type=int, default=128, help="MiB each writer writes (default 128)")
    parser.add_argument("--iops-jobs", type=int, default=16, help="Parallel random-read processes (default 16)")
    parser.add_argument("--duration", type=float, default=15, help="Seconds of random reads (default 15)")
    parser.add_argument("--timeout", type=int, default=1500, help="Seconds for the benchmark (default 1500)")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "qos_throughput", "tests": tests}
    host, code = mounted_host(args, result, KEYS)
    if host is None:
        print(json.dumps(result, indent=2))
        return code or 0

    try:
        token_path = token_file(run_id_of(args.mount_point))
        limits = fs_limits(host, (mount_of(host, args.mount_point) or {}).get("source", ""), token_path)
        bench = run_python(
            host,
            BENCH,
            [args.mount_point, str(args.jobs), str(args.file_mib), str(args.iops_jobs), str(args.duration)],
            timeout=args.timeout,
        )
        write_mbps, read_mbps = round(bench["write_mbps"], 1), round(bench["read_mbps"], 1)
        measured = min(write_mbps, read_mbps)
        iops = round(bench["iops"])
        note = "the Filesystem API has no QoS class to request" + (f"; {limits}" if limits else "")
        tests["bandwidth_meets_min"] = {
            "passed": measured >= args.min_mbps,
            "measured_mbps": measured,
            "min_mbps": args.min_mbps,
            "write_mbps": write_mbps,
            "read_mbps": read_mbps,
            "message": f"{args.jobs} x {args.file_mib} MiB O_DIRECT; {note}",
        }
        tests["iops_meets_min"] = {
            "passed": iops >= args.min_iops,
            "measured_iops": iops,
            "min_iops": args.min_iops,
            "message": f"{args.iops_jobs} processes of random 4 KiB O_DIRECT reads for {args.duration:g}s; {note}",
        }
        for key, field, floor, unit in (
            ("bandwidth_meets_min", "measured_mbps", args.min_mbps, "MB/s"),
            ("iops_meets_min", "measured_iops", args.min_iops, "IOPS"),
        ):
            if not tests[key]["passed"]:
                tests[key]["error"] = f"measured {tests[key][field]:g} {unit}, below the {floor:g} {unit} minimum"
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
