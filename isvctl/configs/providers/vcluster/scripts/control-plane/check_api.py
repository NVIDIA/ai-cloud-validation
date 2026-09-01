#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-FileCopyrightText: Copyright (c) 2026 vCluster Labs
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

"""Check vCluster control plane API connectivity and health.

Runs `kubectl cluster-info` against the Control Plane Cluster to verify
the API server is reachable and authentication succeeds.

Required JSON output:
{
    "success":    bool   - true if API is reachable and auth passes,
    "platform":   str    - "control_plane",
    "account_id": str    - API server URL (serves as the account/cluster ID),
    "tests": {
        "auth":       {"passed": bool},
        "kubernetes": {"passed": bool},
        ...one entry per requested service...
    },
    "error": str  - (optional) present when success is false
}

Usage:
    python check_api.py --region vcluster --services compute,identity,kubernetes
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _kubeconfig_env() -> dict[str, str]:
    env = os.environ.copy()
    host_kc = env.get("VCLUSTER_HOST_KUBECONFIG") or env.get("KUBECONFIG", "")
    if host_kc:
        env["KUBECONFIG"] = host_kc
    return env


def _run(cmd: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check vCluster control plane API health")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    parser.add_argument(
        "--services",
        default="compute,identity,kubernetes",
        help="Comma-separated list of services to probe",
    )
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",")]

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "account_id": "",
        "tests": {},
    }

    if DEMO_MODE:
        result["account_id"] = "https://kubernetes.default.svc"
        result["tests"]["auth"] = {"passed": True}
        for svc in services:
            result["tests"][svc] = {"passed": True}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        # Retrieve API server URL
        rc, stdout, stderr = _run(["kubectl", "cluster-info"], env)
        if rc != 0:
            result["error"] = f"kubectl cluster-info failed: {stderr}"
            print(json.dumps(result, indent=2))
            return 1

        # Extract the control plane URL from the first line of cluster-info output
        server_url = ""
        for line in stdout.splitlines():
            if "control plane" in line.lower() or "master" in line.lower():
                # Strip ANSI escape codes and extract the URL
                import re

                clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
                parts = clean.split()
                for part in parts:
                    if part.startswith("https://"):
                        server_url = part
                        break
                break

        if not server_url:
            # Fallback: read from kubeconfig
            rc2, out2, _ = _run(
                ["kubectl", "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"],
                env,
            )
            server_url = out2 if rc2 == 0 else "unknown"

        result["account_id"] = server_url
        result["tests"]["auth"] = {"passed": True}

        # Verify each requested service with a lightweight check
        for svc in services:
            if svc == "kubernetes":
                rc3, _, _ = _run(["kubectl", "get", "--raw", "/readyz"], env)
                result["tests"][svc] = {"passed": rc3 == 0}
            elif svc == "identity":
                rc3, _, _ = _run(["kubectl", "get", "--raw", "/api/v1/namespaces"], env)
                result["tests"][svc] = {"passed": rc3 == 0}
            else:
                # Generic: just confirm API responds
                rc3, _, _ = _run(["kubectl", "get", "--raw", "/readyz"], env)
                result["tests"][svc] = {"passed": rc3 == 0}

        result["success"] = all(v["passed"] for v in result["tests"].values())

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
