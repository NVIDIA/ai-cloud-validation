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

"""Create a test ServiceAccount and generate a bound token (access key).

Maps "access key" to a Kubernetes ServiceAccount + token in the vCluster
namespace on the Control Plane Cluster.

Required JSON output:
{
    "success":           bool - true if SA and token created,
    "platform":          str  - "control_plane",
    "username":          str  - ServiceAccount name,
    "user_id":           str  - ServiceAccount name,
    "access_key_id":     str  - ServiceAccount name (public credential id),
    "secret_access_key": str  - bound token (secret credential value),
    "error":             str  - (optional) present when success is false
}

Usage:
    python create_access_key.py --region vcluster
"""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

SA_NAME = "isv-validation-sa"
TOKEN_DURATION = "1h"


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
    parser = argparse.ArgumentParser(description="Create vCluster ServiceAccount access key")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    args = parser.parse_args()  # noqa: F841

    ns = os.environ.get("VCLUSTER_NAMESPACE", "vcluster-isv-validation")
    sa_name = f"{SA_NAME}-{int(time.time())}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "username": "",
        "user_id": "",
        "access_key_id": "",
        "secret_access_key": "",
    }

    if DEMO_MODE:
        result["username"] = "isv-validation-sa-demo"
        result["user_id"] = "isv-validation-sa-demo"
        result["access_key_id"] = "isv-validation-sa-demo"
        result["secret_access_key"] = "demo-token-abc123"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        # Ensure namespace exists (the vCluster namespace typically already exists)
        rc_ns, _, _ = _run(["kubectl", "get", "namespace", ns], env)
        if rc_ns != 0:
            rc_create, _, stderr_create = _run(["kubectl", "create", "namespace", ns], env)
            if rc_create != 0:
                result["error"] = f"Failed to create namespace '{ns}': {stderr_create}"
                print(json.dumps(result, indent=2))
                return 1

        # Create ServiceAccount
        rc, _, stderr = _run(["kubectl", "create", "serviceaccount", sa_name, "-n", ns], env)
        if rc != 0:
            result["error"] = f"Failed to create ServiceAccount: {stderr}"
            print(json.dumps(result, indent=2))
            return 1

        # Bind the ServiceAccount to the built-in 'view' ClusterRole so it can
        # authenticate and perform read-only cluster-level operations (e.g. list namespaces).
        # This simulates the minimum credential a tenant needs to verify API access.
        crb_name = f"{sa_name}-view"
        rc3, _, stderr3 = _run(
            [
                "kubectl",
                "create",
                "clusterrolebinding",
                crb_name,
                "--clusterrole=view",
                f"--serviceaccount={ns}:{sa_name}",
            ],
            env,
        )
        if rc3 != 0:
            result["error"] = f"Failed to create ClusterRoleBinding: {stderr3}"
            print(json.dumps(result, indent=2))
            return 1

        # Generate a bound token
        rc2, token, stderr2 = _run(
            ["kubectl", "create", "token", sa_name, "-n", ns, "--duration", TOKEN_DURATION],
            env,
        )
        if rc2 != 0:
            result["error"] = f"Failed to create token: {stderr2}"
            print(json.dumps(result, indent=2))
            return 1

        result["username"] = sa_name
        result["user_id"] = sa_name
        result["access_key_id"] = sa_name
        result["secret_access_key"] = token
        result["success"] = True

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
