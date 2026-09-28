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

"""flock on the mounted filesystem (HSS14-01).

wekafs supports ``flock`` without a mount option. On a file in the run's mount,
with separate processes (separate open file descriptions) on the BM:

  mounted_with_flock  the path is a wekafs mount whose options do not disable
                      flock, and an exclusive lock is granted on it
  flock_exclusive     a holder gets LOCK_EX
  flock_shared        two processes hold LOCK_SH at once
  flock_contention    while one holds LOCK_EX, another's LOCK_EX and LOCK_SH
                      are refused (EWOULDBLOCK), a LOCK_EX is refused while
                      LOCK_SH is held, and LOCK_EX is granted once released

Skips without a BM.

Usage:
    python flock_mount_test.py --instance-id bm.xxx --key-file /tmp/key --mount-point /mnt/isv-a1b2c3

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "flock_mount",
    "tests": {
        "mounted_with_flock": {"passed": true, "mount_options": "rw,relatime,..."},
        "flock_exclusive":    {"passed": true},
        "flock_shared":       {"passed": true},
        "flock_contention":   {"passed": true}
    }
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import log
from common.wekafs import fill_missing, mount_of, mounted_host, redact, run_python

KEYS = ("mounted_with_flock", "flock_exclusive", "flock_shared", "flock_contention")
NO_FLOCK_OPTIONS = ("noflock", "localflock", "nolock")

# Runs as root on the BM. try_lock forks a process that opens the file itself.
FLOCK = """
import fcntl, json, os, sys
path = os.path.join(sys.argv[1], ".isv-flock-" + os.urandom(4).hex())
open(path, "w").close()

def try_lock(mode):
    pid = os.fork()
    if pid == 0:
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            os._exit(0)
        except BlockingIOError:
            os._exit(1)
        except Exception:
            os._exit(2)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

out = {}
holder = os.open(path, os.O_RDWR)
try:
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out["exclusive"] = True
    except OSError as e:
        out["exclusive"] = False
        out["error"] = f"LOCK_EX refused: {e}"
    if out["exclusive"]:
        out["ex_blocks_ex"] = try_lock(fcntl.LOCK_EX) == 1
        out["ex_blocks_sh"] = try_lock(fcntl.LOCK_SH) == 1
        fcntl.flock(holder, fcntl.LOCK_UN)
        out["ex_after_release"] = try_lock(fcntl.LOCK_EX) == 0
        fcntl.flock(holder, fcntl.LOCK_SH | fcntl.LOCK_NB)
        out["shared"] = try_lock(fcntl.LOCK_SH) == 0
        out["sh_blocks_ex"] = try_lock(fcntl.LOCK_EX) == 1
        fcntl.flock(holder, fcntl.LOCK_UN)
finally:
    os.close(holder)
    os.remove(path)
print(json.dumps(out))
"""


def main() -> int:
    """Probe exclusive, shared, and contended flock on the mount.

    Returns:
        0 when the probe ran (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird flock mount test (HSS14-01)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "flock_mount", "tests": tests}
    host, code = mounted_host(args, result, KEYS)
    if host is None:
        print(json.dumps(result, indent=2))
        return code or 0

    try:
        options = (mount_of(host, args.mount_point) or {}).get("options", "")
        probe = run_python(host, FLOCK, [args.mount_point])
        exclusive = bool(probe.get("exclusive"))
        disabled = [o for o in options.split(",") if o in NO_FLOCK_OPTIONS]
        tests["mounted_with_flock"] = {"passed": exclusive and not disabled, "mount_options": options}
        if disabled:
            tests["mounted_with_flock"]["error"] = f"mounted with {','.join(disabled)}"
        elif not exclusive:
            tests["mounted_with_flock"]["error"] = redact(probe.get("error")) or "flock refused on the mount"
        tests["flock_exclusive"] = {"passed": exclusive}
        if not exclusive:
            tests["flock_exclusive"]["error"] = redact(probe.get("error")) or "LOCK_EX refused"
        shared = bool(probe.get("shared"))
        tests["flock_shared"] = {"passed": shared}
        if not shared:
            tests["flock_shared"]["error"] = "a second process was refused LOCK_SH while LOCK_SH was held"
        checks = {
            "ex_blocks_ex": "LOCK_EX granted to a second process while LOCK_EX was held",
            "ex_blocks_sh": "LOCK_SH granted to a second process while LOCK_EX was held",
            "sh_blocks_ex": "LOCK_EX granted to a second process while LOCK_SH was held",
            "ex_after_release": "LOCK_EX refused after the holder released it",
        }
        broken = [message for key, message in checks.items() if not probe.get(key)]
        tests["flock_contention"] = {"passed": exclusive and not broken}
        if broken or not exclusive:
            tests["flock_contention"]["error"] = "; ".join(broken) or "no lock to contend for"
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
