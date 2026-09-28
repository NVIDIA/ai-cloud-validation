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

"""Mount the storage run's filesystem on the test BM over wekafs (feeds HSS07-01 and the I/O checks).

Runs after ``setup`` when a provisioned BM is configured (``BM_INSTANCE_ID`` +
``BM_KEY_FILE``, as the network suite takes them):

1. Creates ``isv-fs-<run_id>-mount`` (1 GiB) through the Filesystem API.
2. Installs the filesystem client from the storage endpoint's own distribution
   route (``<storageEndpoint>/dist/v1/install``) when ``mount.wekafs`` is missing;
   an installed client is reused and always left installed.
3. Hands the filesystem's ``authCredentialsBase64`` to the BM over SSH stdin
   (``common.wekafs.deliver_token``), which checks its auth-token shape and
   writes it to a suite-owned file, ``/root/.weka/isv-<run_id>.json`` - never
   the tenant's own ``/root/.weka/auth-token.json``. The credential is held in
   memory only: never in argv, a log, a local file, or this step's JSON.
4. Confirms the token sees the filesystem under its API name, mounts it
   with ``mount -t wekafs -o auth_token_path=<that file>,... <backend>/<name>
   /mnt/isv-<run_id>`` (UDP mode by default, ``--mount-options``), and writes,
   reads back, and removes a probe file.

Without a BM the step skips ("no BM configured for mount checks") and the mount
checks skip with it. Any failure after the token was handed to the BM removes it
best-effort before the (redacted) error is reported, and the checks that need
the mount then fail with it rather than skip. ``teardown`` unmounts, removes the
run's own token file, and deletes the filesystem.

Usage:
    python setup_mount.py --run-id a1b2c3 --instance-id bm.xxx --key-file /tmp/key

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "setup_mount",
    "mounted": true,
    "mount_point": "/mnt/isv-a1b2c3",
    "client_version": "5.1.0",
    "client_installed": false,
    "fs_id": "filesystem.xxx",
    "fs_name": "isv-fs-a1b2c3-mount",
    "fs_type": "wekafs"
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.filesystems import create_ready, fs_name, mount_endpoint
from common.firebird_client import FirebirdClient, log
from common.probes import running_host, skipped
from common.wekafs import (
    DEFAULT_MOUNT_OPTIONS,
    FS_TYPE,
    INSTALL_TIMEOUT,
    MOUNT_TIMEOUT,
    NO_BM,
    bm_configured,
    client_version,
    install_client,
    mount_filesystem,
    mount_point,
    probe_read_write,
    redact,
    remove_token,
    token_file,
)


def main() -> int:
    """Create, mount, and verify the run's filesystem on the BM.

    Returns:
        0 when mounted (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Mount the Firebird storage run's filesystem on a BM over wekafs")
    parser.add_argument("--run-id", required=True, help="Run ID from the setup step")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--capacity-gib", type=int, default=1, help="Capacity of the mounted filesystem (default 1)")
    parser.add_argument("--mount-options", default=DEFAULT_MOUNT_OPTIONS, help="wekafs mount options (default net=udp)")
    parser.add_argument("--install-timeout", type=int, default=INSTALL_TIMEOUT, help="Seconds for the client install")
    parser.add_argument("--mount-timeout", type=int, default=MOUNT_TIMEOUT, help="Seconds for the mount")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds for the filesystem create (default 900)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "setup_mount",
        "mounted": False,
        "mount_point": "",
        "client_version": "",
    }
    if not bm_configured(args):
        print(json.dumps(skipped(result, NO_BM), indent=2))
        return 0

    host = None
    token_may_be_written = False
    try:
        client = FirebirdClient()
        host = running_host(client, args.instance_id, args.ssh_user, args.key_file)
        created: list[str] = []
        fs = create_ready(client, fs_name(args.run_id, "mount"), args.capacity_gib, args.timeout, created)
        result.update(fs_id=str(fs.get("id", "")), fs_name=str(fs.get("name", "")))
        _, endpoint = mount_endpoint(client, result["fs_id"])

        version = client_version(host)
        result["client_installed"] = not version
        if not version:
            log("  installing the Weka client from the storage endpoint")
            started = time.monotonic()
            version = install_client(host, endpoint, args.install_timeout)
            log(f"  Weka client {version} installed in {int(time.monotonic() - started)}s")
        else:
            log(f"  Weka client {version} already installed")
        result["client_version"] = version

        path = mount_point(args.run_id)
        log(f"  mounting {result['fs_name']} at {path}")
        token_may_be_written = True  # from here on, a failure may leave our token on the BM
        mount_filesystem(client, host, fs, path, args.mount_options, args.mount_timeout)
        probe = probe_read_write(host, path)
        if not probe.get("ok"):
            raise RuntimeError(f"{path} is mounted but a probe file did not read back what was written")
        result.update(mounted=True, mount_point=path, fs_type=FS_TYPE, success=True)
        log(f"  mounted at {path}; a {probe.get('bytes')}-byte probe file wrote, read back, and was removed")
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")
        if token_may_be_written and host is not None:
            try:
                remove_token(host, token_file(args.run_id))
            except Exception as cleanup_error:
                log(f"  could not remove the Weka token after the failed mount: {redact(cleanup_error)}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
