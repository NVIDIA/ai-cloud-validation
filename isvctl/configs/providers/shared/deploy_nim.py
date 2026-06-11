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

"""Deploy a NIM inference container on a remote host via SSH.

Pulls and runs the NIM container with GPU support, waits for the health
endpoint, and outputs connection details as JSON.

Usage:
    python deploy_nim.py --host 54.1.2.3 --key-file /tmp/key.pem
    python deploy_nim.py --host 54.1.2.3 --key-file /tmp/key.pem --model meta/llama-3.2-1b-instruct
    python deploy_nim.py --host 54.1.2.3 --key-file /tmp/key.pem --port 8000 --timeout 600

Output JSON:
{
    "success": true,
    "platform": "vm",
    "container_id": "abc123...",
    "container_name": "isv-nim",
    "model": "meta/llama-3.2-3b-instruct",
    "image": "nvcr.io/nim/meta/llama-3.2-3b-instruct:latest",
    "endpoint": "http://localhost:8000",
    "port": 8000,
    "health_ready": true,
    "host": "54.1.2.3",
    "key_file": "/tmp/key.pem",
    "ssh_user": "ubuntu"
}

Requires: paramiko
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # providers/shared/ (for ssh_paramiko)
from ssh_paramiko import run_cmd, ssh_connect


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy NIM container on remote host")
    parser.add_argument("--host", required=True, help="Remote host IP/hostname")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
    parser.add_argument("--user", default="ubuntu", help="SSH username")
    parser.add_argument("--model", default="meta/llama-3.2-1b-instruct", help="NIM model name")
    parser.add_argument("--tag", default="latest", help="Container image tag")
    parser.add_argument("--port", type=int, default=8000, help="Host port to expose NIM on")
    parser.add_argument("--container-name", default="isv-nim", help="Docker container name")
    parser.add_argument(
        "--ngc-api-key",
        default=os.environ.get("NGC_API_KEY", "") or os.environ.get("NGC_NIM_API_KEY", ""),
        help="NGC API key for pulling NIM images",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Seconds to wait for NIM health endpoint",
    )
    args = parser.parse_args()

    image = f"nvcr.io/nim/{args.model}:{args.tag}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "skipped": False,
        "container_id": None,
        "container_name": args.container_name,
        "model": args.model,
        "image": image,
        "endpoint": f"http://localhost:{args.port}",
        "port": args.port,
        "health_ready": False,
        "host": args.host,
        "key_file": args.key_file,
        "ssh_user": args.user,
    }

    if not args.ngc_api_key:
        print("NGC_API_KEY not set, skipping NIM deployment", file=sys.stderr)
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "NGC_API_KEY not set"
        print(json.dumps(result, indent=2))
        return 0

    ssh = None
    try:
        ssh = ssh_connect(args.host, args.user, args.key_file)

        # Log in to NGC registry
        print("Logging in to NGC registry...", file=sys.stderr)
        exit_code, _, stderr_out = run_cmd(
            ssh,
            f"echo '{args.ngc_api_key}' | docker login nvcr.io -u '$oauthtoken' --password-stdin 2>&1",
        )
        if exit_code != 0:
            result["error"] = f"NGC login failed: {stderr_out}"
            print(json.dumps(result, indent=2))
            return 1

        # Clean up previous containers/images to free disk space
        run_cmd(ssh, f"docker rm -f {args.container_name} 2>/dev/null || true")
        run_cmd(ssh, "docker system prune -af 2>/dev/null || true")

        # Launch NIM container
        print(f"Launching NIM container: {image}", file=sys.stderr)
        docker_cmd = (
            f"docker run -d --gpus all"
            f" --name {args.container_name}"
            f" -p {args.port}:8000"
            f" -e NGC_API_KEY='{args.ngc_api_key}'"  # NIM container expects NGC_API_KEY
            f" {image}"
        )
        exit_code, stdout, stderr_out = run_cmd(ssh, docker_cmd, timeout=1200)
        if exit_code != 0:
            result["error"] = f"docker run failed: {stderr_out}"
            print(json.dumps(result, indent=2))
            return 1

        container_id = stdout.strip()[:12]
        result["container_id"] = container_id
        print(f"Container started: {container_id}", file=sys.stderr)

        # Poll health endpoint until ready
        print(f"Waiting for NIM health endpoint (timeout: {args.timeout}s)...", file=sys.stderr)
        deadline = time.time() + args.timeout
        poll_interval = 10
        while time.time() < deadline:
            exit_code, stdout, _ = run_cmd(
                ssh,
                f"curl -sf http://localhost:{args.port}/v1/health/ready 2>/dev/null && echo OK || echo WAIT",
            )
            if "OK" in stdout:
                result["health_ready"] = True
                print("NIM is ready.", file=sys.stderr)
                break
            # Check container is still running
            exit_code, stdout, _ = run_cmd(ssh, f"docker inspect -f '{{{{.State.Running}}}}' {args.container_name}")
            if stdout.strip() != "true":
                _, logs, _ = run_cmd(ssh, f"docker logs --tail 30 {args.container_name} 2>&1")
                result["error"] = f"Container exited unexpectedly. Logs:\n{logs}"
                print(json.dumps(result, indent=2))
                return 1
            time.sleep(poll_interval)
        else:
            _, logs, _ = run_cmd(ssh, f"docker logs --tail 30 {args.container_name} 2>&1")
            result["error"] = f"Health endpoint not ready after {args.timeout}s. Logs:\n{logs}"
            print(json.dumps(result, indent=2))
            return 1

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
    finally:
        if ssh:
            ssh.close()

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
