#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 vCluster Labs
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Retrieve details for a specific vCluster tenant cluster.

Parses `vcluster list --namespace <ns> -o json` output to find the named
tenant cluster and returns its metadata.

Required JSON output:
{
    "success":      bool - true if the tenant was found,
    "platform":     str  - "control_plane",
    "tenant_name":  str  - vCluster name,
    "description":  str  - human-readable description,
    "status":       str  - vCluster status (e.g. "Running"),
    "error":        str  - (optional) present when success is false
}

Usage:
    python get_tenant.py --group-name <tenant-name> --region vcluster
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
    parser = argparse.ArgumentParser(description="Get vCluster tenant cluster details")
    parser.add_argument("--group-name", required=True, help="Tenant cluster name")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    args = parser.parse_args()

    ns = os.environ.get("VCLUSTER_NAMESPACE", args.group_name)

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenant_name": "",
        "description": "vCluster tenant cluster",
        "status": "",
    }

    if DEMO_MODE:
        result["tenant_name"] = args.group_name
        result["tenant_id"] = args.group_name
        result["status"] = "Running"
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
            result["error"] = f"vcluster list failed: {stderr}"
            print(json.dumps(result, indent=2))
            return 1

        try:
            vclusters = json.loads(out) if out else []
        except json.JSONDecodeError:
            vclusters = []

        target = next(
            (vc for vc in vclusters if vc.get("Name") == args.group_name or vc.get("name") == args.group_name),
            None,
        )

        if target is None:
            result["error"] = f"vCluster '{args.group_name}' not found in namespace '{ns}'"
            print(json.dumps(result, indent=2))
            return 1

        result["tenant_name"] = target.get("Name") or target.get("name", args.group_name)
        result["tenant_id"] = result["tenant_name"]
        result["status"] = target.get("Status") or target.get("status", "unknown")
        result["description"] = "vCluster tenant cluster"
        result["success"] = True

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
