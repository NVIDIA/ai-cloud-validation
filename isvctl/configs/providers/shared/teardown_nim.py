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

"""Tear down a NIM inference container on a remote host via SSH.

Stops and removes the NIM container, optionally removes the image.

Usage:
    python teardown_nim.py --host 54.1.2.3 --key-file /tmp/key.pem
    python teardown_nim.py --host 54.1.2.3 --key-file /tmp/key.pem --remove-image

Output JSON:
{
    "success": true,
    "platform": "vm",
    "container_removed": true,
    "image_removed": false,
    "container_name": "isv-nim"
}

Requires: paramiko
"""

import argparse
import json
import os
import re
import sys
from typing import Any

import paramiko

_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def ssh_connect(host: str, user: str, key_file: str) -> paramiko.SSHClient:
    """Create SSH connection to remote host."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        key_filename=key_file,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def run_cmd(ssh: paramiko.SSHClient, command: str, timeout: int = 60) -> tuple[int, str, str]:
    """Execute command via SSH and return (exit_code, stdout, stderr)."""
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, stdout.read().decode(), stderr.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser(description="Tear down NIM container on remote host")
    parser.add_argument("--host", required=True, help="Remote host IP/hostname")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
    parser.add_argument("--user", default="ubuntu", help="SSH username")
    parser.add_argument("--container-name", default="isv-nim", help="Docker container name")
    parser.add_argument("--remove-image", action="store_true", help="Also remove the container image")
    parser.add_argument(
        "--ngc-api-key",
        default=os.environ.get("NGC_API_KEY", "") or os.environ.get("NGC_NIM_API_KEY", ""),
        help=(
            "NGC API key (defaults to NGC_API_KEY / NGC_NIM_API_KEY env var). "
            "When absent the step short-circuits with success=True, skipped=True "
            "to mirror deploy_nim's policy-skip — deploy was a no-op, so there "
            "is no container to tear down."
        ),
    )
    args = parser.parse_args()

    if not _CONTAINER_NAME_RE.match(args.container_name):
        print(
            json.dumps({"success": False, "error": f"Invalid container name: {args.container_name!r}"}),
        )
        return 1

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "skipped": False,
        "container_removed": False,
        "image_removed": False,
        "container_name": args.container_name,
    }

    # Policy-skip on missing NGC_API_KEY (mirrors deploy_nim's policy-skip
    # shape — when deploy_nim was skipped because the operator's env has
    # no NGC entitlement, there is no container to tear down). Equivalent
    # symmetric handling so the documented "leave NGC_API_KEY unset to
    # opt out of NIM coverage" path stays green end-to-end.
    if not args.ngc_api_key:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "NGC_API_KEY not set (deploy_nim was skipped, nothing to tear down)"
        print(json.dumps(result, indent=2))
        return 0

    # Sentinel-skip path: when the producing step (launch / start /
    # reboot) was skipped or failed, the provider config forwards
    # "none" / "null" / "" sentinels rather than dropping the argv pair.
    # Treat any sentinel host or key as "no instance was ever ready for
    # SSH" and emit the canonical policy-skip JSON (rc=0, success=True,
    # skipped=True) so the orchestrator's StepSuccessCheck does not
    # turn a failed-setup run red on teardown.
    _SENTINELS = {"none", "null", ""}
    if args.host.strip().lower() in _SENTINELS or args.key_file.strip().lower() in _SENTINELS:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = (
            f"sentinel host/key forwarded (host={args.host!r}, key_file={args.key_file!r}); "
            "upstream lifecycle step did not produce a reachable instance, nothing to tear down"
        )
        print(json.dumps(result, indent=2))
        return 0

    ssh = None
    try:
        ssh = ssh_connect(args.host, args.user, args.key_file)

        try:
            # Get image name before removing container (for optional image removal)
            image_name = None
            if args.remove_image:
                exit_code, stdout, _ = run_cmd(
                    ssh, f"docker inspect -f '{{{{.Config.Image}}}}' {args.container_name} 2>/dev/null"
                )
                if exit_code == 0:
                    image_name = stdout.strip()

            # Stop and remove container
            print(f"Stopping container: {args.container_name}", file=sys.stderr)
            exit_code, stdout_out, stderr_out = run_cmd(ssh, f"docker rm -f {args.container_name}")
            already_gone = "No such container" in stderr_out or "No such container" in stdout_out
            result["container_removed"] = exit_code == 0 or already_gone

            # Optionally remove image
            if args.remove_image and image_name:
                print(f"Removing image: {image_name}", file=sys.stderr)
                exit_code, _, _ = run_cmd(ssh, f"docker rmi {image_name} 2>&1", timeout=120)
                result["image_removed"] = exit_code == 0

            result["success"] = result["container_removed"]
        finally:
            if ssh is not None and hasattr(ssh, "close"):
                ssh.close()

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
