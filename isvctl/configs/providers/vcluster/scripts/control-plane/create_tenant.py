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

"""Create a vCluster tenant cluster.

Maps "tenant" to a vCluster instance in the configured namespace on the
Control Plane Cluster.

Required JSON output:
{
    "success":     bool - true if the tenant cluster was created,
    "platform":    str  - "control_plane",
    "tenant_name": str  - vCluster name,
    "tenant_id":   str  - vCluster name,
    "error":       str  - (optional) present when success is false
}

Usage:
    python create_tenant.py --region vcluster
"""

import argparse
import json
import os
import subprocess
import sys
import time
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
    parser = argparse.ArgumentParser(description="Create vCluster tenant cluster")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    args = parser.parse_args()  # noqa: F841

    tenant_name = f"isv-tenant-{int(time.time())}"
    # Each vCluster must be in its own namespace; derive it from the tenant name so
    # the control-plane suite can run independently of the k8s suite.
    ns = os.environ.get("VCLUSTER_NAMESPACE", tenant_name)

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenant_name": "",
        "tenant_id": "",
    }

    if DEMO_MODE:
        result["tenant_name"] = "isv-tenant-demo"
        result["tenant_id"] = "isv-tenant-demo"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        # Create the vCluster tenant cluster (--connect=false: no auto port-forward)
        rc, _, stderr = _run(
            ["vcluster", "create", tenant_name, "--namespace", ns, "--connect=false"],
            env,
        )
        if rc != 0:
            result["error"] = f"vcluster create failed: {stderr}"
            print(json.dumps(result, indent=2))
            return 1

        # Wait until Running (poll up to 5 minutes)
        import time as _time

        for _ in range(60):
            rc2, out2, _ = _run(
                ["vcluster", "list", "--namespace", ns, "--output", "json"],
                env,
            )
            if rc2 == 0 and out2:
                try:
                    items = json.loads(out2) or []
                    match = next((v for v in items if v.get("Name") == tenant_name), None)
                    if match and match.get("Status") == "Running":
                        break
                except json.JSONDecodeError:
                    pass
            _time.sleep(5)
        else:
            result["error"] = f"vCluster '{tenant_name}' did not reach Running status within 5 minutes"
            print(json.dumps(result, indent=2))
            return 1

        result["tenant_name"] = tenant_name
        result["tenant_id"] = tenant_name
        result["success"] = True

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
