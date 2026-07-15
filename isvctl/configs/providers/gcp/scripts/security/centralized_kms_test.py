#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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

"""Verify encrypted resources reference centralized Cloud KMS keys (SEC09-03).

The AWS reference lists KMS keys, samples encrypted EBS volumes and EKS
clusters, and resolves each to an Enabled KMS key.

GCP differs in two ways:

  * There is no flat key listing. Walk locations -> key rings -> crypto keys ->
    crypto key versions via
    ``google.cloud.kms_v1.KeyManagementServiceClient``. The enabled state lives
    on the CryptoKeyVersion (``CryptoKeyVersion.state == ENABLED``), not on the
    key, so a key counts as present only when it has at least one enabled
    version.
  * CMEK references differ per service: Compute disks
    (``disk_encryption_key.kms_key_name``), GCS buckets
    (``encryption.default_kms_key_name``), and GKE clusters
    (``database_encryption.key_name``).

This script is read-only and bounded. It has NO skip path: a project with no
Cloud KMS usage is an honest hard failure, not a fabricated pass.

Usage:
    python3 centralized_kms_test.py --region us-central1 --project my-project

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "centralized_kms_test",
    "kms_keys_total": 3,
    "encrypted_resources_inspected": 5,
    "non_kms_resources": 0,
    "tests": {
        "kms_service_reachable": {"passed": true},
        "kms_keys_present": {"passed": true},
        "all_encrypted_resources_use_kms": {"passed": true}
    }
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from common.kms import iter_kms_locations
from google.api_core import exceptions as gax
from google.cloud import compute_v1, container_v1, kms_v1, storage

# Bound the per-service sampling so the check does not become an account-wide
# audit (mirrors the AWS reference's bounded sampling).
MAX_RESOURCES_PER_SERVICE = 25


def _count_enabled_keys(client: kms_v1.KeyManagementServiceClient, project: str) -> tuple[int, set[str]]:
    """Return (enabled-key count, set of enabled key resource paths).

    Walks locations -> key rings -> crypto keys -> versions. A key counts only
    when it has at least one ENABLED version (GCP places enabled-state on the
    version, not the key). The returned set is used to resolve CMEK references.
    """
    enabled = 0
    enabled_keys: set[str] = set()
    enabled_state = kms_v1.CryptoKeyVersion.CryptoKeyVersionState.ENABLED
    permission_denials: list[tuple[str, gax.PermissionDenied]] = []
    for location in iter_kms_locations(client, project):
        try:
            for key_ring in client.list_key_rings(parent=location.name):
                for crypto_key in client.list_crypto_keys(parent=key_ring.name):
                    for version in client.list_crypto_key_versions(parent=crypto_key.name):
                        if version.state == enabled_state:
                            enabled += 1
                            enabled_keys.add(crypto_key.name)
                            break
        except gax.PermissionDenied as exc:
            # Keep walking so later locations still produce diagnostic evidence,
            # but never certify an account-wide inventory that was incomplete.
            permission_denials.append((location.name, exc))
    if permission_denials:
        denied_locations = ", ".join(location for location, _exc in permission_denials)
        raise gax.PermissionDenied(
            f"Cloud KMS inventory is incomplete because access was denied for location(s): {denied_locations}"
        ) from permission_denials[0][1]
    return enabled, enabled_keys


def _kms_key_resolves(kms_key_name: str, enabled_keys: set[str]) -> bool:
    """Return True iff a CMEK reference resolves to an enabled tenant CryptoKey.

    Resource CMEK references include a trailing ``/cryptoKeyVersions/<n>``; the
    key paths collected from ``list_crypto_keys`` do not. Match on the key path
    prefix so a versioned reference still resolves.
    """
    if not kms_key_name:
        return False
    return any(kms_key_name == key or kms_key_name.startswith(f"{key}/") for key in enabled_keys)


def _inspect_disks(project: str, enabled_keys: set[str], details: list[str]) -> int:
    """Sample CMEK-encrypted Compute disks and return the number inspected."""
    inspected = 0
    client = compute_v1.DisksClient()
    for zone, scoped in client.aggregated_list(project=project):
        for disk in getattr(scoped, "disks", None) or []:
            enc = getattr(disk, "disk_encryption_key", None)
            kms_key = getattr(enc, "kms_key_name", "") if enc else ""
            if not kms_key:
                continue  # default / Google-managed encryption -- not a CMEK resource
            if inspected >= MAX_RESOURCES_PER_SERVICE:
                return inspected
            inspected += 1
            if not _kms_key_resolves(kms_key, enabled_keys):
                details.append(f"disk:{zone}/{disk.name}: CMEK key did not resolve to an enabled tenant CryptoKey")
    return inspected


def _inspect_buckets(project: str, enabled_keys: set[str], details: list[str]) -> int:
    """Sample CMEK-encrypted GCS buckets and return the number inspected."""
    inspected = 0
    client = storage.Client(project=project)
    for bucket in client.list_buckets():
        kms_key = getattr(bucket, "default_kms_key_name", "") or ""
        if not kms_key:
            continue  # default Google-managed encryption -- not a CMEK resource
        if inspected >= MAX_RESOURCES_PER_SERVICE:
            return inspected
        inspected += 1
        if not _kms_key_resolves(kms_key, enabled_keys):
            details.append(f"bucket:{bucket.name}: CMEK key did not resolve to an enabled tenant CryptoKey")
    return inspected


def _inspect_clusters(project: str, enabled_keys: set[str], details: list[str]) -> int:
    """Sample GKE clusters with database (application-layer) CMEK and return the count."""
    inspected = 0
    client = container_v1.ClusterManagerClient()
    response = client.list_clusters(parent=f"projects/{project}/locations/-")
    for cluster in getattr(response, "clusters", None) or []:
        db_enc = getattr(cluster, "database_encryption", None)
        kms_key = getattr(db_enc, "key_name", "") if db_enc else ""
        if not kms_key:
            continue  # application-layer secrets encryption not configured with a CMEK
        if inspected >= MAX_RESOURCES_PER_SERVICE:
            return inspected
        inspected += 1
        if not _kms_key_resolves(kms_key, enabled_keys):
            details.append(f"gke:{cluster.name}: CMEK key did not resolve to an enabled tenant CryptoKey")
    return inspected


@handle_gcp_errors
def main() -> int:
    """Walk Cloud KMS, sample CMEK resources, and emit JSON result."""
    parser = argparse.ArgumentParser(description="Centralized KMS test (SEC09-03)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "centralized_kms_test",
        "kms_keys_total": 0,
        "encrypted_resources_inspected": 0,
        "non_kms_resources": 0,
        "tests": {
            "kms_service_reachable": {"passed": False},
            "kms_keys_present": {"passed": False},
            "all_encrypted_resources_use_kms": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)
        kms_client = kms_v1.KeyManagementServiceClient()

        # 1. Reach Cloud KMS and count enabled keys (state lives on the version).
        kms_keys_total, enabled_keys = _count_enabled_keys(kms_client, project)
        result["kms_keys_total"] = kms_keys_total
        result["tests"]["kms_service_reachable"] = {
            "passed": True,
            "message": "Cloud KMS key inventory enumerated",
        }
        result["tests"]["kms_keys_present"] = {
            "passed": kms_keys_total >= 1,
            "message" if kms_keys_total >= 1 else "error": (
                f"{kms_keys_total} enabled Cloud KMS key(s) present"
                if kms_keys_total >= 1
                else "No enabled Cloud KMS keys found in this project"
            ),
        }

        # 2. Sample CMEK-encrypted resources and resolve each reference to a key.
        details: list[str] = []
        inspected = 0
        inspected += _inspect_disks(project, enabled_keys, details)
        inspected += _inspect_buckets(project, enabled_keys, details)
        inspected += _inspect_clusters(project, enabled_keys, details)

        result["encrypted_resources_inspected"] = inspected
        result["non_kms_resources"] = len(details)
        all_kms = inspected >= 1 and not details
        result["tests"]["all_encrypted_resources_use_kms"] = {
            "passed": all_kms,
            "message" if all_kms else "error": (
                f"{inspected} encrypted resource(s) resolved to enabled Cloud KMS keys"
                if all_kms
                else (
                    f"Encrypted resources without resolvable Cloud KMS keys: {details}"
                    if details
                    else "No CMEK-encrypted resources were sampled"
                )
            ),
        }
        result["success"] = all(t["passed"] for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
