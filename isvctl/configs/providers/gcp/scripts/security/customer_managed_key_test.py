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

"""Verify customer-managed key / BYOK support on GCP (SEC09-04).

The AWS reference describes or creates a customer-managed KMS key, asserts it is
Enabled + ENCRYPT_DECRYPT + KeyManager=CUSTOMER, runs an encrypt/decrypt
roundtrip, creates a CMEK-encrypted EBS volume, and reads back its KmsKeyId.

The GCP analog differs in a few ways:

  * GCP has no ``KeyManager`` field. A CryptoKey that lives in the tenant's own
    key ring and is retrievable via ``get_crypto_key`` IS the customer-managed
    key; "customer-managed" is inferred from tenant-key-ring ownership.
  * Cloud KMS key rings and crypto keys cannot be hard-deleted. A self-created
    key is cleaned up best-effort by scheduling its primary version for
    destruction; the key and ring resources persist.
  * CMEK on a Persistent Disk is create-time only, so the roundtrip provisions a
    fresh CMEK disk and reads back ``disk_encryption_key.kms_key_name``.
  * The Compute Engine service agent has no implicit access to a customer key, so
    a self-created key is granted ``roles/cloudkms.cryptoKeyEncrypterDecrypter``
    for the service agent before the disk is created (an operator ``--key-id`` is
    never re-permissioned).

When ``--key-id`` names an existing CryptoKey it is used as-is (never destroyed).
Otherwise the script creates a symmetric ENCRYPT_DECRYPT key in a tenant key
ring and schedules its version for destruction on the way out.

Usage:
    python3 customer_managed_key_test.py --region us-central1 --project my-project
    python3 customer_managed_key_test.py --region us-central1 --key-id \\
        projects/p/locations/us-central1/keyRings/r/cryptoKeys/k

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "customer_managed_key_test",
    "key_id": "projects/p/locations/us-central1/keyRings/r/cryptoKeys/k",
    "encrypted_resource_id": "isv-sec09-disk-...",
    "key_created": true,
    "disk_created": true,
    "tests": {
        "customer_managed_key_available": {"passed": true},
        "key_manager_is_customer": {"passed": true},
        "encrypt_decrypt_roundtrip": {"passed": true},
        "resource_encrypted_with_customer_key": {"passed": true},
        "provider_managed_key_not_used": {"passed": true}
    }
}
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    narrow_region_to_zone,
    resolve_project,
    unique_suffix,
    wait_for_zonal_op,
)
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import delete_disk
from google.api_core import exceptions as gax
from google.cloud import compute_v1, kms_v1, resourcemanager_v3

# A self-created key ring is reused across same-run re-runs (rings/keys are not
# deletable, so a deterministic per-run ring avoids unbounded accumulation),
# while the crypto key itself carries extra entropy so a prior run that
# scheduled its key version for destruction never leaves this run with a key
# that has no ENABLED version to encrypt with.
_KEY_RING_BASE = "isv-sec09-cmk-ring"
_KEY_BASE = "isv-sec09-cmk"
_DISK_BASE = "isv-sec09-disk"

# Roundtrip payload. Plain marker text, never key material.
_PLAINTEXT = b"isv-customer-managed-key-validation"

# The Compute Engine service agent that must be allowed to use a self-created
# CMEK key before a CMEK disk can be inserted.
_GCE_AGENT_TEMPLATE = "service-{number}@compute-system.iam.gserviceaccount.com"
_KMS_ENCRYPTER_ROLE = "roles/cloudkms.cryptoKeyEncrypterDecrypter"


def _normalize_optional(value: str | None) -> str:
    """Return a stripped value, treating empty template strings as absent."""
    return (value or "").strip()


def _get_or_create_key_ring(client: kms_v1.KeyManagementServiceClient, location_parent: str, ring_id: str) -> str:
    """Return the resource path of a tenant key ring, creating it idempotently.

    Cloud KMS key rings cannot be deleted, so a deterministic per-run ring is
    reused on a same-run re-run: an ``AlreadyExists`` on create means the ring is
    already present and usable.
    """
    ring_path = f"{location_parent}/keyRings/{ring_id}"
    try:
        client.create_key_ring(parent=location_parent, key_ring_id=ring_id, key_ring=kms_v1.KeyRing())
    except gax.AlreadyExists:
        pass
    return ring_path


def _create_crypto_key(client: kms_v1.KeyManagementServiceClient, ring_path: str, key_id: str) -> str:
    """Create a symmetric ENCRYPT_DECRYPT CryptoKey and return its resource path."""
    crypto_key = kms_v1.CryptoKey(
        purpose=kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT,
        version_template=kms_v1.CryptoKeyVersionTemplate(
            algorithm=kms_v1.CryptoKeyVersion.CryptoKeyVersionAlgorithm.GOOGLE_SYMMETRIC_ENCRYPTION,
        ),
    )
    created = client.create_crypto_key(parent=ring_path, crypto_key_id=key_id, crypto_key=crypto_key)
    return created.name


def _key_is_customer_managed(client: kms_v1.KeyManagementServiceClient, key_name: str) -> bool:
    """Return True iff the key is retrievable as a tenant-owned ENCRYPT_DECRYPT key.

    GCP has no ``KeyManager`` field: a CryptoKey that lives in the tenant project
    and is retrievable via ``get_crypto_key`` with the ENCRYPT_DECRYPT purpose is
    the customer-managed key (the provider default at-rest key is not a Cloud KMS
    resource and is never returned here).
    """
    key = client.get_crypto_key(name=key_name)
    return key.purpose == kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT


def _encrypt_decrypt_roundtrip(client: kms_v1.KeyManagementServiceClient, key_name: str) -> bool:
    """Encrypt then decrypt a small payload with the key; return byte-equality."""
    encrypted = client.encrypt(request={"name": key_name, "plaintext": _PLAINTEXT})
    decrypted = client.decrypt(request={"name": key_name, "ciphertext": encrypted.ciphertext})
    return decrypted.plaintext == _PLAINTEXT


def _grant_gce_service_agent(project: str, key_name: str) -> None:
    """Bind the Compute Engine service agent as encrypter/decrypter on the key.

    Unlike AWS (where the account root key policy lets EBS use a CMEK by
    default), GCP grants the Compute Engine service agent no implicit access to a
    customer key, so a CMEK disk insert fails with a ``cryptoKeyVersions
    .useToEncrypt`` permission denial until this binding exists. Only ever called
    for a self-created key; an operator ``--key-id`` is never re-permissioned.
    """
    proj = resourcemanager_v3.ProjectsClient().get_project(name=f"projects/{project}")
    project_number = proj.name.split("/", 1)[1]  # "projects/<number>" -> "<number>"
    agent = _GCE_AGENT_TEMPLATE.format(number=project_number)

    kms_client = kms_v1.KeyManagementServiceClient()
    policy = kms_client.get_iam_policy(request={"resource": key_name})
    member = f"serviceAccount:{agent}"
    for binding in policy.bindings:
        if binding.role == _KMS_ENCRYPTER_ROLE and member in binding.members:
            return  # already granted
    policy.bindings.add(role=_KMS_ENCRYPTER_ROLE, members=[member])
    kms_client.set_iam_policy(request={"resource": key_name, "policy": policy})


def _create_cmek_disk(project: str, zone: str, disk_name: str, key_name: str) -> None:
    """Insert a CMEK-encrypted Persistent Disk and block until the op is DONE."""
    disk = compute_v1.Disk()
    disk.name = disk_name
    disk.size_gb = 1
    disk.type_ = f"projects/{project}/zones/{zone}/diskTypes/pd-standard"
    disk.labels = {"created-by": "isvtest"}
    disk.disk_encryption_key = compute_v1.CustomerEncryptionKey(kms_key_name=key_name)

    client = compute_v1.DisksClient()
    op = client.insert(project=project, zone=zone, disk_resource=disk)
    op_name = getattr(op, "name", None) or getattr(op, "operation", "")
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=300)


def _disk_uses_key(project: str, zone: str, disk_name: str, key_name: str) -> bool:
    """Read back the disk and return True iff its CMEK key matches ``key_name``.

    A CMEK reference on a created disk carries a trailing
    ``/cryptoKeyVersions/<n>`` that the bare key path lacks, so match on the key
    path prefix.
    """
    disk = compute_v1.DisksClient().get(project=project, zone=zone, disk=disk_name)
    enc = getattr(disk, "disk_encryption_key", None)
    actual = getattr(enc, "kms_key_name", "") if enc else ""
    return bool(actual) and (actual == key_name or actual.startswith(f"{key_name}/"))


@handle_gcp_errors
def main() -> int:
    """Run the BYOK / customer-managed-key checks and emit JSON result."""
    parser = argparse.ArgumentParser(description="Customer-managed key / BYOK test (SEC09-04)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--key-id", default="", help="Existing tenant CryptoKey resource path (operator-supplied)")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "customer_managed_key_test",
        "key_id": "",
        "encrypted_resource_id": "",
        "key_created": False,
        "disk_created": False,
        "tests": {
            "customer_managed_key_available": {"passed": False},
            "key_manager_is_customer": {"passed": False},
            "encrypt_decrypt_roundtrip": {"passed": False},
            "resource_encrypted_with_customer_key": {"passed": False},
            "provider_managed_key_not_used": {"passed": False},
        },
    }

    project = ""
    zone = ""
    disk_name = ""
    key_name = ""
    operator_key = _normalize_optional(args.key_id)
    # Bound before the try so the finally block can reuse this warm client for
    # the best-effort key-version destroy instead of building a cold one (a
    # fresh gRPC channel constructed in finally is the flakiest call here).
    kms_client: kms_v1.KeyManagementServiceClient | None = None

    try:
        project = resolve_project(args.project)
        zone = narrow_region_to_zone(args.region) if args.region else narrow_region_to_zone("us-central1")
        kms_client = kms_v1.KeyManagementServiceClient()
        location = args.region or "us-central1"
        location_parent = f"projects/{project}/locations/{location}"

        # 1. Use an operator key as-is, or create a fresh tenant CryptoKey.
        if operator_key:
            key_name = operator_key
        else:
            ring_path = _get_or_create_key_ring(kms_client, location_parent, unique_suffix(_KEY_RING_BASE))
            # Extra entropy on the key so a prior run that scheduled its key
            # version for destruction never hands this run a key with no
            # ENABLED version (rings/keys are not deletable).
            fresh_key_id = f"{unique_suffix(_KEY_BASE)}-{uuid.uuid4().hex[:6]}"
            key_name = _create_crypto_key(kms_client, ring_path, fresh_key_id)
            result["key_created"] = True  # stamp ownership before any wait
        result["key_id"] = key_name

        # 2. The key must be retrievable as a tenant ENCRYPT_DECRYPT key.
        is_customer = _key_is_customer_managed(kms_client, key_name)
        result["tests"]["customer_managed_key_available"] = {
            "passed": is_customer,
            "message" if is_customer else "error": (
                f"Customer-managed CryptoKey is available: {key_name}"
                if is_customer
                else f"CryptoKey {key_name} is not a usable ENCRYPT_DECRYPT key"
            ),
        }
        # Tenant-key-ring ownership IS the GCP signal for customer-managed (no
        # KeyManager field), so these two derive from the same observation.
        result["tests"]["key_manager_is_customer"] = {
            "passed": is_customer,
            "message": "CryptoKey lives in a tenant key ring (customer-managed)",
        }
        result["tests"]["provider_managed_key_not_used"] = {
            "passed": is_customer,
            "message": "A tenant Cloud KMS key was used, not the Google-managed default key",
        }

        if is_customer:
            # 3. Encrypt/decrypt roundtrip with the key.
            roundtrip_ok = _encrypt_decrypt_roundtrip(kms_client, key_name)
            result["tests"]["encrypt_decrypt_roundtrip"] = {
                "passed": roundtrip_ok,
                "message" if roundtrip_ok else "error": (
                    "Cloud KMS encrypt/decrypt roundtrip succeeded"
                    if roundtrip_ok
                    else "Cloud KMS decrypt did not match the original payload"
                ),
            }

            # 4. Create a CMEK Persistent Disk and read back its key reference.
            if not operator_key:
                # The Compute Engine service agent needs explicit use of a
                # self-created key before the CMEK disk insert is permitted.
                _grant_gce_service_agent(project, key_name)
            disk_name = unique_suffix(_DISK_BASE)
            # Stamp ownership BEFORE the async insert/wait: if the wait raises
            # (timeout / DONE-with-errors) the disk may still exist, so the
            # finally block and the teardown safety-net must both see it. A
            # delete of a never-created disk is an idempotent NotFound no-op.
            result["disk_created"] = True
            result["encrypted_resource_id"] = disk_name
            _create_cmek_disk(project, zone, disk_name, key_name)

            disk_ok = _disk_uses_key(project, zone, disk_name, key_name)
            result["tests"]["resource_encrypted_with_customer_key"] = {
                "passed": disk_ok,
                "message" if disk_ok else "error": (
                    f"Persistent Disk {disk_name} is encrypted with the customer-managed key"
                    if disk_ok
                    else f"Persistent Disk {disk_name} does not reference the expected customer-managed key"
                ),
            }
    except Exception as e:
        result["error"] = str(e)
    finally:
        cleanup_errors: list[str] = []
        # Delete the CMEK disk this run created (best-effort, retry-on-transient).
        # delete_disk waits for the async zonal delete op to reach DONE, so a
        # clean (no cleanup_errors) result means the disk is observably gone, not
        # merely that the delete call was accepted.
        if result["disk_created"] and project and zone and disk_name:
            if not delete_with_retry(
                delete_disk,
                project,
                zone,
                disk_name,
                resource_desc=f"disk {disk_name}",
            ):
                cleanup_errors.append(f"delete disk {disk_name}")
        # Never destroy an operator-supplied key. A self-created key's resources
        # (ring + key) cannot be hard-deleted, so schedule its primary version
        # for destruction best-effort to stop its material from being usable.
        # This is deliberately kept OUT of cleanup_errors (unlike the disk
        # above): Cloud KMS rings/keys are permanently undeletable, so a failed
        # version-destroy leaks no deletable resource, and the dedicated teardown
        # step (teardown.py:_sweep_kms_keys) is the authoritative reclamation
        # that re-destroys every ENABLED version of this run's keys. Folding a
        # transient Cloud KMS error on this redundant scrub into success flipped
        # the gate red while every subtest passed and the disk was reclaimed, so
        # it is retried (delete_with_retry) and, if still unfinished, only
        # logged. The warm client is reused; only built fresh as a fallback.
        if result["key_created"] and not operator_key and key_name:
            destroy_client = kms_client or kms_v1.KeyManagementServiceClient()
            if not delete_with_retry(
                destroy_client.destroy_crypto_key_version,
                name=f"{key_name}/cryptoKeyVersions/1",
                resource_desc=f"key version for {key_name}",
            ):
                print(
                    f"warning: best-effort destroy of key version for {key_name} did not "
                    "complete; teardown will reclaim it (KMS rings/keys are undeletable)",
                    file=sys.stderr,
                )

        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors

    # Recompute success only after the try/except/finally completes: the verdict
    # is grounded in the subtests AND fails when cleanup leaked a resource (the
    # AWS oracle and this domain's teardown both fold cleanup_errors into it).
    result["success"] = all(t["passed"] for t in result["tests"].values()) and not result.get("cleanup_errors")
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
