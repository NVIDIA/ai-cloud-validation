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
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # providers/shared/ (for ssh_paramiko)
from ssh_paramiko import run_cmd, ssh_connect

CONTAINER_REMOVE_TIMEOUT = 240
IMAGE_REMOVE_TIMEOUT = 240

_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Tear down NIM container on remote host")
    parser.add_argument("--host", required=True, help="Remote host IP/hostname")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
    parser.add_argument("--user", default="ubuntu", help="SSH username")
    parser.add_argument("--container-name", default="isv-nim", help="Docker container name")
    parser.add_argument("--remove-image", action="store_true", help="Also remove the container image")
    parser.add_argument("--skip", action="store_true", help="Skip teardown because NIM deployment did not run")
    parser.add_argument("--skip-reason", default="NIM deployment skipped", help="Reason to report when skipping")
    args = parser.parse_args()

    if not _CONTAINER_NAME_RE.match(args.container_name):
        print(
            json.dumps({"success": False, "error": f"Invalid container name: {args.container_name!r}"}),
        )
        return 1

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "container_removed": False,
        "image_removed": False,
        "container_name": args.container_name,
    }

    if args.skip:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = args.skip_reason
        result["message"] = args.skip_reason
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
                    ssh,
                    f"docker inspect -f '{{{{.Config.Image}}}}' {args.container_name} 2>/dev/null",
                    operation="docker inspect",
                )
                if exit_code == 0:
                    image_name = stdout.strip()

            # Stop and remove container
            print(f"Stopping container: {args.container_name}", file=sys.stderr)
            exit_code, stdout_out, stderr_out = run_cmd(
                ssh,
                f"docker rm -f {args.container_name}",
                timeout=CONTAINER_REMOVE_TIMEOUT,
                operation="docker rm",
            )
            already_gone = "No such container" in stderr_out or "No such container" in stdout_out
            result["container_removed"] = exit_code == 0 or already_gone

            # Optionally remove image
            if args.remove_image and image_name:
                print(f"Removing image: {image_name}", file=sys.stderr)
                exit_code, _, _ = run_cmd(
                    ssh,
                    f"docker rmi {image_name} 2>&1",
                    timeout=IMAGE_REMOVE_TIMEOUT,
                    operation="docker rmi",
                )
                result["image_removed"] = exit_code == 0

            result["success"] = result["container_removed"]
        finally:
            if ssh is not None and hasattr(ssh, "close"):
                ssh.close()

    except TimeoutError as e:
        result["error_type"] = "timeout"
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
