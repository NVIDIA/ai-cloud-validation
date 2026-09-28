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

"""uid/gid/project quotas with soft grace and hard enforcement on the mounted filesystem (HSS12-01).

The filesystem's quotas are directory quotas, which is what this check treats
as the project quota; it has no per-uid or per-gid quota, so those two subtests fail
as not supported. On a directory under the run's mount the step sets a quota
(``weka fs quota set <dir> --soft --hard --grace``), then:

  soft_quota_grace   a write past the soft limit but under the hard limit succeeds
  hard_quota_blocks  a further write past the hard limit is refused
  project_quota_enforced  the quota was set and both of the above held

When the token's role cannot set quotas, the three quota subtests fail with
the role error the mount reports. The quota and the directory are removed on
every path.
Skips without a BM.

Usage:
    python quota_enforcement_test.py --instance-id bm.xxx --key-file /tmp/key --mount-point /mnt/isv-a1b2c3

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "quota_enforcement",
    "tests": {
        "uid_quota_enforced":     {"passed": false, "supported": false, "error": "not supported: ..."},
        "gid_quota_enforced":     {"passed": false, "supported": false, "error": "not supported: ..."},
        "project_quota_enforced": {"passed": true, "soft_mb": 10, "hard_mb": 20},
        "soft_quota_grace":       {"passed": true},
        "hard_quota_blocks":      {"passed": true}
    }
}
"""

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import log
from common.wekafs import WEKA_TOKEN_ENV, fill_missing, mounted_host, redact, remote, run_id_of, token_file, token_role

IDENTITY_REASON = "not supported: Weka quotas are per directory; there is no per-uid or per-gid quota"
QUOTA_KEYS = ("project_quota_enforced", "soft_quota_grace", "hard_quota_blocks")
KEYS = ("uid_quota_enforced", "gid_quota_enforced", *QUOTA_KEYS)
SOFT_MB, HARD_MB = 10, 20
OVER_SOFT_MIB, PAST_HARD_MIB = 15, 10  # 15 MiB > 10 MB soft, < 20 MB hard; +10 MiB > 20 MB hard


def _weka(token_path: str) -> str:
    """Return the ``sudo`` prefix that runs the ``weka`` CLI against the run's own token."""
    return f"sudo env {WEKA_TOKEN_ENV}={shlex.quote(token_path)} weka"


def _write(host: Any, mount_point: str, path: str, mib: int) -> tuple[bool, str, str]:
    """Append ``mib`` MiB to ``path`` (O_DIRECT, fsync); return (succeeded, stdout, stderr).

    ``mount_point`` is checked with ``findmnt`` in the SAME remote command
    immediately before the write, so a mount dropped between steps can never
    let an overfill write land on the BM's root disk instead.
    """
    command = (
        f"findmnt -n -t wekafs -M {shlex.quote(mount_point)} >/dev/null && "
        f"sudo dd if=/dev/zero of={shlex.quote(path)} bs=1M count={mib} oflag=direct,append conv=notrunc,fsync status=none"
    )
    code, stdout, stderr = remote(host, command, timeout=300)
    return code == 0, stdout, stderr


def main() -> int:
    """Set a directory quota, probe soft and hard limits, and remove it.

    Returns:
        0 when the probes ran (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird quota enforcement test (HSS12-01)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    parser.add_argument("--grace", default="1m", help="Soft-limit grace period (default 1m)")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {
        key: {"passed": False, "supported": False, "error": IDENTITY_REASON}
        for key in ("uid_quota_enforced", "gid_quota_enforced")
    }
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "quota_enforcement", "tests": tests}
    host, code = mounted_host(args, result, KEYS)
    if host is None:
        print(json.dumps(result, indent=2))
        return code or 0

    token_path = token_file(run_id_of(args.mount_point))
    directory = os.path.join(args.mount_point, ".isv-quota-" + os.urandom(4).hex())
    quoted = shlex.quote(directory)
    quota_set = False
    try:
        code, stdout, stderr = remote(host, f"sudo mkdir {quoted}")
        if code != 0:
            raise RuntimeError(f"could not create {directory}: {redact(stderr or stdout)}")
        code, stdout, stderr = remote(
            host,
            f"{_weka(token_path)} fs quota set {quoted} --soft {SOFT_MB}MB --hard {HARD_MB}MB "
            f"--grace {shlex.quote(args.grace)}",
            timeout=120,
        )
        if code != 0:
            role = token_role(host, token_path)
            error = f"weka fs quota set failed: {redact(stderr or stdout) or f'exit {code}'}" + (
                f" (token role {role})" if role else ""
            )
            tests.update({key: {"passed": False, "error": error} for key in QUOTA_KEYS})
        else:
            quota_set = True
            data = os.path.join(directory, "data")
            soft_ok, soft_stdout, soft_stderr = _write(host, args.mount_point, data, OVER_SOFT_MIB)
            tests["soft_quota_grace"] = {"passed": soft_ok, "written_mib": OVER_SOFT_MIB, "soft_mb": SOFT_MB}
            if not soft_ok:
                soft_error = redact(soft_stderr or soft_stdout) or "unknown error"
                tests["soft_quota_grace"]["error"] = f"a write past the soft limit was refused: {soft_error}"
            hard_ok, hard_stdout, hard_stderr = _write(host, args.mount_point, data, PAST_HARD_MIB)
            edquot = "disk quota exceeded" in (hard_stderr or "").lower()
            blocked = not hard_ok and edquot
            tests["hard_quota_blocks"] = {"passed": blocked, "hard_mb": HARD_MB}
            if blocked:
                tests["hard_quota_blocks"]["message"] = redact(hard_stderr or hard_stdout)
            elif not hard_ok:
                tests["hard_quota_blocks"]["error"] = (
                    "dd failed but not with a quota error (EDQUOT): "
                    f"{redact(hard_stderr or hard_stdout) or 'unknown error'}"
                )
            else:
                tests["hard_quota_blocks"]["error"] = (
                    f"{OVER_SOFT_MIB + PAST_HARD_MIB} MiB fit under a {HARD_MB} MB hard limit"
                )
            enforced = soft_ok and blocked
            tests["project_quota_enforced"] = {"passed": enforced, "soft_mb": SOFT_MB, "hard_mb": HARD_MB}
            if not enforced:
                tests["project_quota_enforced"]["error"] = "the directory quota was set but not enforced as configured"
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")
    finally:
        cleanup = (f"{_weka(token_path)} fs quota unset {quoted}; " if quota_set else "") + f"sudo rm -rf {quoted}"
        code, stdout, stderr = remote(host, cleanup, timeout=120)
        if code != 0:
            result["cleanup_errors"] = [f"{directory}: {redact(stderr or stdout)}"]
            result["success"] = False

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
