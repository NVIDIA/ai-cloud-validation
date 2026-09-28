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

"""Root squash on the mounted filesystem (HSS13-01).

The Filesystem API has no root-squash setting, so ``enable_root_squash`` and
``disable_root_squash`` fail as not supported (the error names the mount
token's role). The observable half still runs: root creates a file in the
run's mount and the owner the filesystem records is read back.

  root_squashed    root's file is owned by someone other than uid 0
  root_unsquashed  root's file is owned by uid 0 (squash off, the default)

Skips without a BM.

Usage:
    python root_squash_test.py --instance-id bm.xxx --key-file /tmp/key --mount-point /mnt/isv-a1b2c3

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "root_squash",
    "tests": {
        "enable_root_squash":  {"passed": false, "supported": false, "error": "not supported: ..."},
        "root_squashed":       {"passed": false, "owner_uid": 0, "error": "..."},
        "disable_root_squash": {"passed": false, "supported": false, "error": "not supported: ..."},
        "root_unsquashed":     {"passed": true, "owner_uid": 0}
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
from common.wekafs import fill_missing, mounted_host, redact, run_id_of, run_python, token_file, token_role

KEYS = ("enable_root_squash", "root_squashed", "disable_root_squash", "root_unsquashed")
TOGGLE_REASON = "not supported: the Filesystem API has no root-squash setting"

# Runs as root on the BM: creates a file, reads its owner back from a fresh stat, removes it.
OWNER = """
import json, os, sys
path = os.path.join(sys.argv[1], ".isv-root-" + os.urandom(4).hex())
try:
    with open(path, "w") as f:
        f.write("root")
    st = os.stat(path)
finally:
    if os.path.exists(path):
        os.remove(path)
print(json.dumps({"uid": st.st_uid, "gid": st.st_gid}))
"""


def main() -> int:
    """Report the root-squash toggles and the observed owner of a root-created file.

    Returns:
        0 when the probe ran (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird root-squash test (HSS13-01)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "root_squash", "tests": tests}
    host, code = mounted_host(args, result, KEYS)
    if host is None:
        print(json.dumps(result, indent=2))
        return code or 0

    try:
        token_path = token_file(run_id_of(args.mount_point))
        role = token_role(host, token_path)
        reason = TOGGLE_REASON + (f" (the mount token's Weka role is {role})" if role else "")
        for key in ("enable_root_squash", "disable_root_squash"):
            tests[key] = {"passed": False, "supported": False, "error": reason}
        owner = run_python(host, OWNER, [args.mount_point])
        uid = int(owner["uid"])
        tests["root_squashed"] = {"passed": uid != 0, "owner_uid": uid}
        tests["root_unsquashed"] = {"passed": uid == 0, "owner_uid": uid}
        if uid == 0:
            tests["root_squashed"]["error"] = "a file root created is owned by uid 0: root is not squashed"
            tests["root_unsquashed"]["message"] = "root keeps uid 0 on the mount (squash off, the default)"
        else:
            tests["root_unsquashed"]["error"] = f"a file root created is owned by uid {uid}: root is squashed"
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
