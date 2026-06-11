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

"""Delete a vCluster tenant cluster.

Teardown counterpart to create_tenant.py. Removes the vCluster instance from
the Control Plane Cluster namespace using the vcluster CLI.

Required JSON output:
{
    "success":           bool - true if the tenant was deleted (or was already gone),
    "platform":          str  - "control_plane",
    "resources_deleted": list - deleted resource names,
    "message":           str  - human-readable result summary,
    "error":             str  - (optional) present when success is false
}

Usage:
    python delete_tenant.py --group-name <tenant-name> --region vcluster
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
    parser = argparse.ArgumentParser(description="Delete vCluster tenant cluster")
    parser.add_argument("--group-name", required=True, help="Tenant cluster name")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    args = parser.parse_args()

    # Namespace is derived from the tenant name (one vCluster per namespace).
    # Honour explicit override via VCLUSTER_NAMESPACE for backwards-compatibility.
    ns = os.environ.get("VCLUSTER_NAMESPACE", args.group_name)

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "resources_deleted": [],
        "message": "",
    }

    if DEMO_MODE:
        result["resources_deleted"] = [args.group_name]
        result["message"] = f"vCluster '{args.group_name}' deleted (demo)"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        rc, stdout, stderr = _run(
            ["vcluster", "delete", args.group_name, "--namespace", ns],
            env,
        )

        # vcluster delete exits non-zero if the cluster doesn't exist; treat
        # "not found" as a successful teardown.  The vCluster CLI may print the
        # not-found message to either stdout or stderr depending on the version.
        combined = (stdout + stderr).lower()
        not_found = (
            "not found" in combined
            or "does not exist" in combined
            or "no vcluster" in combined
            or "couldn't find" in combined
        )

        if rc != 0 and not not_found:
            result["error"] = f"vcluster delete failed: {stderr}"
            print(json.dumps(result, indent=2))
            return 1

        result["resources_deleted"] = [f"vcluster/{args.group_name}"]
        result["message"] = (
            f"vCluster '{args.group_name}' deleted from namespace '{ns}'."
            if rc == 0
            else f"vCluster '{args.group_name}' was already absent from namespace '{ns}'."
        )
        result["success"] = True

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
