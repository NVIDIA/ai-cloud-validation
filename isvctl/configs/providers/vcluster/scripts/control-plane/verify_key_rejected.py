#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 vCluster Labs
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Verify that a disabled ServiceAccount token is rejected by the API.

Attempts to use the original bearer token to call the API. After the SA has
been disabled (ClusterRoleBinding deleted by disable_access_key.py), requests
with that token must return a 403 Forbidden response because the token still
authenticates but the RBAC permissions have been revoked.

Required JSON output:
{
    "success":    bool - true if the token was correctly rejected (401 or 403),
    "platform":   str  - "control_plane",
    "rejected":   bool - true if the API returned 401 or 403,
    "error_code": str  - "401" or "403",
    "error":      str  - (optional) present when success is false
}

Usage:
    python verify_key_rejected.py --access-key-id <sa-name> \
        --secret-access-key <original-token> --region vcluster
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
    parser = argparse.ArgumentParser(description="Verify revoked vCluster ServiceAccount token is rejected")
    parser.add_argument("--access-key-id", required=True, help="ServiceAccount name")
    parser.add_argument("--secret-access-key", required=True, help="Original bound token")
    parser.add_argument("--region", required=True, help="Region label (use 'vcluster')")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "rejected": False,
        "error_code": "",
    }

    if DEMO_MODE:
        result["rejected"] = True
        result["error_code"] = "403"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        env = _kubeconfig_env()

        # Attempt to use the disabled token - we WANT this to fail
        rc, out, stderr = _run(
            ["kubectl", "--token", args.secret_access_key, "get", "--raw", "/api/v1/namespaces"],
            env,
        )

        combined = (out + stderr).lower()

        if rc != 0 and (
            "unauthorized" in combined or "401" in combined or "forbidden" in combined or "403" in combined
        ):
            # Token was correctly rejected with an auth error (401/403)
            error_code = "403" if "403" in combined or "forbidden" in combined else "401"
            result["rejected"] = True
            result["error_code"] = error_code
            result["success"] = True
        elif rc != 0:
            # Non-auth error (TLS failure, API unavailable, etc.) - do not treat
            # as a successful rejection; surface it as an actual failure so
            # outages are not silently masked.
            result["error"] = f"Token verification failed with a non-auth error (exit {rc}): {stderr[:200]}"
        else:
            # Token still works - this is a failure
            result["error"] = (
                "Token was NOT rejected: the disabled token still authenticates successfully. "
                "This indicates the disable_access_key step did not fully revoke the credential."
            )
            result["rejected"] = False

    except Exception as exc:  # pylint: disable=broad-except
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
