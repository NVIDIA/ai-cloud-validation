#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Report the Kubernetes versions EKS offers (K8S02-01).

``DescribeClusterVersions`` is EKS's version catalogue: one entry per minor,
carrying the patch EKS ships for it and whether that minor is still supported.

Only supported minors are reported. An ``UNSUPPORTED`` entry is one EKS still
names but no longer maintains, so counting it would let a provider satisfy the
maintenance-window requirement with versions it has stopped standing behind.

Usage:
    python list_k8s_versions.py --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "kubernetes",
    "offered_versions": ["1.37.0", "1.36.4", "1.35.8"]
}
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Minors EKS names but no longer maintains.
UNSUPPORTED = "UNSUPPORTED"


def offered_versions(region: str) -> list[str]:
    """Return the Kubernetes versions EKS currently supports, newest first."""
    client = boto3.client("eks", region_name=region)
    paginator = client.get_paginator("describe_cluster_versions")

    versions = []
    for page in paginator.paginate():
        for entry in page.get("clusterVersions") or []:
            if entry.get("versionStatus") == UNSUPPORTED:
                continue
            # The patch is what EKS actually runs for that minor; the bare
            # minor is the fallback for an entry that omits it.
            version = entry.get("kubernetesPatchVersion") or entry.get("clusterVersion")
            if version:
                versions.append(str(version))
    return versions


def main() -> int:
    """Print the EKS version catalogue as the step's JSON result."""
    parser = argparse.ArgumentParser(description="Report the Kubernetes versions EKS offers")
    parser.add_argument("--region", required=True, help="AWS region to query")
    args = parser.parse_args()

    result: dict[str, object] = {"success": True, "platform": "kubernetes", "offered_versions": []}
    try:
        versions = offered_versions(args.region)
    except (BotoCoreError, ClientError) as exc:
        result["success"] = False
        result["error"] = f"Could not read the EKS version catalogue in {args.region}: {exc}"
    else:
        if versions:
            result["offered_versions"] = versions
        else:
            result["success"] = False
            result["error"] = f"EKS reported no supported Kubernetes versions in {args.region}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
