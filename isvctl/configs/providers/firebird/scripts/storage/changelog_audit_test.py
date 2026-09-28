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

"""Changelog / audit data on the mounted filesystem (HSS15-01).

Reads the mounted filesystem's per-filesystem audit switch (``weka audit fs
status``) with the mount token:

  changelog_enabled  audit is on for the filesystem

The Filesystem API has no setting to turn it on. Audit records are not exposed
through the tenant API, so ``records_file_ops``, ``records_dir_ops``, and
``tracks_uid_gid`` are reported unsupported when auditing is enabled, or
failed with the reason when it is disabled. Skips without a BM.

Usage:
    python changelog_audit_test.py --instance-id bm.xxx --key-file /tmp/key --mount-point /mnt/isv-a1b2c3

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "changelog_audit",
    "tests": {
        "changelog_enabled": {"passed": false, "error": "audit is disabled on filesystem ..."},
        "records_file_ops":  {"passed": false, "error": "..."},
        "records_dir_ops":   {"passed": false, "error": "..."},
        "tracks_uid_gid":    {"passed": false, "error": "..."}
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
from common.wekafs import fill_missing, mount_of, mounted_host, redact, run_id_of, token_file, token_role, weka_json

KEYS = ("changelog_enabled", "records_file_ops", "records_dir_ops", "tracks_uid_gid")
RECORD_KEYS = KEYS[1:]
NO_FEED = "Audit records are not exposed through the tenant API; the record-content subtests are unsupported"


def main() -> int:
    """Read the filesystem's audit state and report the changelog subtests.

    Returns:
        0 when the audit state was read (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird changelog/audit test (HSS15-01)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "changelog_audit", "tests": tests}
    host, code = mounted_host(args, result, KEYS)
    if host is None:
        print(json.dumps(result, indent=2))
        return code or 0

    try:
        token_path = token_file(run_id_of(args.mount_point))
        source = (mount_of(host, args.mount_point) or {}).get("source", "")
        backend, _, name = source.partition("/")
        status = weka_json(host, f"audit fs status -H {shlex.quote(backend)}", token_path)
        entry = next((fs for fs in status if fs.get("name") == name), None)
        if entry is None:
            raise RuntimeError(f"weka audit fs status does not list filesystem {name}")
        enabled = bool(entry.get("audit"))
        tests["changelog_enabled"] = {"passed": enabled, "audit_operations": entry.get("audit_operations_str", "")}
        if enabled:
            reason = NO_FEED
        else:
            role = token_role(host, token_path)
            reason = f"audit is disabled on filesystem {name}; the Filesystem API has no setting to enable it" + (
                f" (the mount token's Weka role is {role})" if role else ""
            )
            tests["changelog_enabled"]["error"] = reason
            reason = f"no changelog to read: {reason}"
        for key in RECORD_KEYS:
            tests[key] = {"passed": False, "error": reason}
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
