#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 vCluster Labs
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Test that a ServiceAccount token can authenticate to the cluster.

Uses the bound token as a bearer credential to call `kubectl --token` and
verifies the API server accepts it.

Required JSON output:
{
    "success":       bool - true if token authenticated successfully,
    "platform":      str  - "control_plane",
    "authenticated": bool - true if the token was accepted,
    "account_id":    str  - API server URL,
    "error":         str  - (optional) present when success is false
}

Usage:
    python test_access_key.py --access-key-id <sa-name> \
        --secret-access-key <token> --region vcluster
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
    parser = argparse.ArgumentParser(description="Test vCluster ServiceAccount token")
    parser.add_argument("--access-key-id", required=True, help="ServiceAccount name")
    parser.add_argument("--secret-access-key", required=True, help="Bound token")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "authenticated": False,
        "account_id": "",
    }

    if DEMO_MODE:
        result["authenticated"] = True
        result["account_id"] = "https://kubernetes.default.svc"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        # Retrieve server URL for reporting
        rc0, server_url, _ = _run(
            ["kubectl", "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"],
            env,
        )
        result["account_id"] = server_url if rc0 == 0 else "unknown"

        # Attempt authentication with the bearer token
        rc, _, stderr = _run(
            ["kubectl", "--token", args.secret_access_key, "get", "--raw", "/api/v1/namespaces"],
            env,
        )
        if rc == 0:
            result["authenticated"] = True
            result["success"] = True
        else:
            result["error"] = f"Token authentication failed: {stderr}"
            result["authenticated"] = False

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
