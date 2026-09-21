#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Report how EKS patches this cluster's control plane (K8S02-02).

AWS patches an EKS control plane itself, but not unconditionally, so the answer
is read from the cluster rather than assumed. Patching follows the minor's
support status and the cluster's own upgrade policy:

- ``STANDARD_SUPPORT``: AWS maintains the minor, so patches land automatically.
- ``EXTENDED_SUPPORT``: patches continue only for a cluster whose upgrade
  policy opted into extended support. A cluster left on ``STANDARD`` is instead
  force-upgraded off the minor, so nothing is patching it where it sits.
- ``UNSUPPORTED``: the minor is not maintained at all.

``current_version`` is the patch EKS ships for the cluster's minor, which is
what a managed control plane on that minor runs. Reporting it from the EKS API
rather than from the cluster keeps it an independent claim - the bound check
compares it against the version the API server itself reports, so a control
plane AWS has not actually rolled forward yet is caught.

Usage:
    python describe_control_plane_patching.py --cluster-name my-cluster --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "kubernetes",
    "automated_patching_enabled": true,
    "current_version": "1.35.8"
}
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

STANDARD_SUPPORT = "STANDARD_SUPPORT"
EXTENDED_SUPPORT = "EXTENDED_SUPPORT"
EXTENDED_POLICY = "EXTENDED"


def patching_posture(cluster_name: str, region: str) -> dict[str, Any]:
    """Return whether EKS patches this control plane, and the patch it ships."""
    client = boto3.client("eks", region_name=region)
    cluster = client.describe_cluster(name=cluster_name)["cluster"]
    minor = str(cluster.get("version") or "")
    support_type = (cluster.get("upgradePolicy") or {}).get("supportType")

    entry = _version_entry(client, minor)
    status = entry.get("versionStatus")
    automated = status == STANDARD_SUPPORT or (status == EXTENDED_SUPPORT and support_type == EXTENDED_POLICY)

    return {
        "automated_patching_enabled": automated,
        "current_version": str(entry.get("kubernetesPatchVersion") or minor),
    }


def _version_entry(client: Any, minor: str) -> dict[str, Any]:
    """Return the catalogue entry for ``minor``, or an empty one if EKS dropped it."""
    paginator = client.get_paginator("describe_cluster_versions")
    for page in paginator.paginate():
        for entry in page.get("clusterVersions") or []:
            if str(entry.get("clusterVersion") or "") == minor:
                return entry
    return {}


def main() -> int:
    """Print the EKS control-plane patching posture as the step's JSON result."""
    parser = argparse.ArgumentParser(description="Report how EKS patches this cluster's control plane")
    parser.add_argument("--cluster-name", required=True, help="EKS cluster to describe")
    parser.add_argument("--region", required=True, help="AWS region the cluster is in")
    args = parser.parse_args()

    result: dict[str, object] = {"success": True, "platform": "kubernetes"}
    try:
        result.update(patching_posture(args.cluster_name, args.region))
    except (BotoCoreError, ClientError, KeyError) as exc:
        result["success"] = False
        result["error"] = f"Could not read the patching posture for EKS cluster {args.cluster_name!r}: {exc}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
