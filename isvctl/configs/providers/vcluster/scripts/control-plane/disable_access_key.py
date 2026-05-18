#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 vCluster Labs
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Disable a ServiceAccount access key by removing its ClusterRoleBinding.

In Kubernetes, there is no "disable" state for ServiceAccount tokens. Disabling
is achieved by deleting the ClusterRoleBinding that grants the ServiceAccount its
permissions. The bound token still authenticates but every API call returns 403
Forbidden, which the validation suite treats as "rejected". The ServiceAccount
itself is removed later by delete_access_key.py.

Required JSON output:
{
    "success": bool - true if the ClusterRoleBinding was removed,
    "platform": str - "control_plane",
    "status":   str - "Inactive",
    "error":    str - (optional) present when success is false
}

Usage:
    python disable_access_key.py --username <sa-name> \
        --access-key-id <sa-name> --region vcluster
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
    parser = argparse.ArgumentParser(description="Disable vCluster ServiceAccount token")
    parser.add_argument("--username", required=True, help="ServiceAccount name")
    parser.add_argument("--access-key-id", required=True, help="ServiceAccount name")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "status": "",
    }

    if DEMO_MODE:
        result["status"] = "Inactive"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        # Disable the access key by removing the ClusterRoleBinding that grants
        # the ServiceAccount its permissions. The bound token still authenticates
        # but any API call returns 403 Forbidden, which the validation suite
        # treats as "rejected". The SA itself is deleted in delete_access_key.py.
        crb_name = f"{args.username}-view"
        rc_crb, _, stderr_crb = _run(
            ["kubectl", "delete", "clusterrolebinding", crb_name,
             "--ignore-not-found=true"],
            env,
        )
        if rc_crb != 0:
            result["error"] = f"Failed to delete ClusterRoleBinding '{crb_name}': {stderr_crb}"
            print(json.dumps(result, indent=2))
            return 1

        result["status"] = "Inactive"
        result["success"] = True

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
