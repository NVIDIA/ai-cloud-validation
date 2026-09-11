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

"""List vCluster tenant clusters and verify the target tenant is present.

Uses `vcluster list --namespace <ns> -o json` which outputs a JSON array of
objects with at minimum the fields: Name, Namespace, Status.

Required JSON output:
{
    "success":       bool - true if the list call succeeded,
    "platform":      str  - "control_plane",
    "found_target":  bool - true if the target tenant appears in the list,
    "target_tenant": str  - the name that was searched for,
    "count":         int  - total number of tenant clusters listed,
    "error":         str  - (optional) present when success is false
}

Usage:
    python list_tenants.py --region vcluster --group-name <tenant-name>
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _kubeconfig_env() -> dict[str, str]:
    """Return a copy of os.environ with KUBECONFIG pointed at the Control Plane Cluster.

    VCLUSTER_HOST_KUBECONFIG wins over KUBECONFIG so tests that override the
    tenant kubeconfig in KUBECONFIG still drive control-plane commands against
    the host cluster where the vCluster CR lives.
    """
    env = os.environ.copy()
    host_kc = env.get("VCLUSTER_HOST_KUBECONFIG") or env.get("KUBECONFIG", "")
    if host_kc:
        env["KUBECONFIG"] = host_kc
    return env


def _run(cmd: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Run ``cmd`` and return ``(exit_code, stripped_stdout, stripped_stderr)``."""
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    """List vCluster tenants in the configured namespace and report whether the target exists."""
    parser = argparse.ArgumentParser(description="List vCluster tenant clusters")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    parser.add_argument("--group-name", required=True, help="Target tenant name to look for")
    args = parser.parse_args()

    ns = os.environ.get("VCLUSTER_NAMESPACE", args.group_name)

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "found_target": False,
        "target_tenant": args.group_name,
        "count": 0,
    }

    if DEMO_MODE:
        result["found_target"] = True
        result["count"] = 1
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        rc, out, stderr = _run(
            ["vcluster", "list", "--namespace", ns, "--output", "json"],
            env,
        )

        if rc != 0:
            # Keep the JSON contract provider-neutral; raw CLI diagnostics
            # go to stderr where the orchestrator can pick them up.
            print(f"vcluster list failed: {stderr}", file=sys.stderr)
            result["error"] = "vcluster list failed"
            print(json.dumps(result, indent=2))
            return 1

        try:
            vclusters = json.loads(out) if out else []
        except json.JSONDecodeError:
            # vcluster list may emit non-JSON when the list is empty
            vclusters = []

        result["count"] = len(vclusters)
        result["found_target"] = any(
            vc.get("Name") == args.group_name or vc.get("name") == args.group_name for vc in vclusters
        )
        result["success"] = True

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
