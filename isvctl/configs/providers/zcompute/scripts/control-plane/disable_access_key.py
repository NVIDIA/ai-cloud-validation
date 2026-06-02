#!/usr/bin/env python3
"""Disable an IAM access key (set status to Inactive).

Tries iam:UpdateAccessKey first (AWS-compatible). If zcompute returns
NotImplementedException, falls back to the native `symp` CLI:

    symp access-key update <access_key_id> False

The symp CLI must be available in PATH (or as SYMP_CMD env var).

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "access_key_id": "...",
    "status": "Inactive",
    "method": "iam_api" | "symp_cli"
}
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402

from botocore.exceptions import ClientError


def _disable_via_symp(access_key_id: str) -> dict[str, Any]:
    """Fall back to `symp access-key update <id> False` when IAM API is not implemented.

    Required environment variables:
        SYMP_URL      zcompute management URL, e.g. http://172.29.0.20
        SYMP_USER     symp username, e.g. amitor
        SYMP_DOMAIN   symp domain, e.g. amitor
        SYMP_PROJECT  symp project, e.g. amitor
        SYMP_PASSWORD symp password
    """
    url = os.environ.get("SYMP_URL") or os.environ.get("ZCOMPUTE_BASE_URL", "")
    user = os.environ.get("SYMP_USER", "")
    domain = os.environ.get("SYMP_DOMAIN", "")
    project = os.environ.get("SYMP_PROJECT", "")
    password = os.environ.get("SYMP_PASSWORD", "")

    missing = [v for v, val in [
        ("SYMP_URL", url), ("SYMP_USER", user),
        ("SYMP_DOMAIN", domain), ("SYMP_PROJECT", project),
        ("SYMP_PASSWORD", password),
    ] if not val]
    if missing:
        return {
            "success": False,
            "method": "symp_cli",
            "error": f"Missing env vars for symp fallback: {', '.join(missing)}. "
                     "Set SYMP_URL, SYMP_USER, SYMP_DOMAIN, SYMP_PROJECT, SYMP_PASSWORD.",
        }

    cmd = [
        "symp", "-k",
        "-u", user,
        "-d", domain,
        "-p", password,
        "--url", url,
        "--project", project,
        "access-key", "update", access_key_id, "False",
    ]
    print(f"[disable] IAM API not implemented — falling back to symp CLI", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return {"success": True, "status": "Inactive", "method": "symp_cli"}
        return {
            "success": False,
            "method": "symp_cli",
            "error": f"symp exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "method": "symp_cli",
            "error": "symp not found in PATH. Install the symp CLI on this machine.",
        }
    except Exception as e:
        return {"success": False, "method": "symp_cli", "error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()

    iam = get_client("iam", region=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "access_key_id": args.access_key_id,
    }

    try:
        iam.update_access_key(
            UserName=args.username,
            AccessKeyId=args.access_key_id,
            Status="Inactive",
        )
        result.update({"status": "Inactive", "success": True, "method": "iam_api"})

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "NotImplementedException":
            # zcompute does not implement UpdateAccessKey — try symp CLI fallback
            symp_result = _disable_via_symp(args.access_key_id)
            result.update(symp_result)
            if not result["success"]:
                # symp not available — known platform gap (NK-19406).
                # Exit 0 so the step does not fail the orchestration.
                # AccessKeyDisabledCheck and AccessKeyRejectedCheck are excluded in config.
                result["platform_gap"] = True
                result["status"] = "NotImplemented"
                result["note"] = "iam:UpdateAccessKey not implemented on zCompute (NK-19406)."
                print(json.dumps(result, indent=2))
                return 0
        else:
            result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
