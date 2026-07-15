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

"""Verify hard tenant isolation across network/data/compute/storage (SEC11-01).

The AWS reference models two tenants as two IAM users in ONE account, each with
its own VPC + KMS CMK + S3 bucket + EC2 instance + EBS volume, then runs
cross-tenant negative probes from tenant A's credentials against tenant B's
resources and asserts each is denied. Three of the four planes prove IAM
default-deny; the network plane is a config-plane inspection (no peering / no
shared route).

The GCP port keeps the same shape: two scoped service accounts in ONE operator
project, each owning its own VPC, Cloud KMS CryptoKey, Cloud Storage bucket, and
Compute Engine instance. Each identity receives resource-level access only to
its own key, bucket, and VM. Positive control probes must first prove that own
access works; symmetric cross-tenant probes then prove the same permission is
denied on the other tenant's resource. This prevents a powerless principal's
default-deny posture from masquerading as tenant isolation.

  * network_isolated: each tenant's VPC carries no cross-tenant peering and no
    custom route toward the other tenant's primary range (config-plane
    inspection, mirrors the AWS orchestrator no-peering / no-shared-route check).
    Two custom-mode VPCs in the same project are mutually unreachable unless a
    peering or an explicit cross-tenant route is wired in.
  * data_isolated: each SA can read its own CryptoKey metadata and is denied the
    other tenant's CryptoKey.
  * compute_isolated: each SA can read and stop its disposable own VM, then is
    denied ``instances.get`` and ``instances.stop`` on the other tenant's VM.
  * storage_isolated: each SA can read its own bucket metadata and is denied the
    other tenant's bucket. Cloud Storage may hide the foreign bucket as a 404.

GCP Cloud KMS key rings and crypto keys cannot be hard-deleted; a self-created
key is cleaned up best-effort by scheduling its version for destruction. Every
other fixture (service accounts, VPCs, buckets, instances) is torn down in a
finally block gated on the created flags. Names carry the run-id suffix and a
``created-by=isvtest`` label (where the proto supports labels) so an operator
sweep can reclaim anything a hard crash leaks.

Usage:
    python3 tenant_isolation_test.py --region us-central1 --project my-project

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "tenant_isolation_test",
    "tenant_a_id": "isv-sec11-a-...",
    "tenant_b_id": "isv-sec11-b-...",
    "sa_created": true,
    "vpc_created": true,
    "kms_key_created": true,
    "bucket_created": true,
    "instance_created": true,
    "tests": {
        "network_isolated": {"passed": true, "message": "..."},
        "data_isolated": {"passed": true, "probes": [...]},
        "compute_isolated": {"passed": true, "probes": [...]},
        "storage_isolated": {"passed": true, "probes": [...]}
    }
}
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

import google.auth
import google.auth.impersonated_credentials
import google.auth.transport.requests
from common.compute import (
    get_instance,
    narrow_region_to_zone,
    resolve_project,
    short_name,
    unique_suffix,
    zone_to_region,
)
from common.errors import delete_with_retry, handle_gcp_errors, modify_iam_policy_with_retry
from common.iam_policy import ensure_unconditional_binding_member, remove_binding_members
from common.network import (
    build_probe_instance,
    delete_instance,
    delete_network,
    delete_subnetwork,
    get_network,
    get_subnetwork,
    insert_instance,
    insert_network,
    insert_subnetwork,
    is_auto_route,
    list_routes_for_network,
    network_peerings,
)
from common.ownership import (
    CREATED_BY_DESCRIPTION,
    CREATED_BY_LABEL,
    CREATED_BY_VALUE,
    has_invocation_label,
    labels_with_invocation,
    new_invocation_id,
    submit_owned_create,
)
from common.result import preserve_success_after_cleanup
from common.service_account import (
    IAM_PROPAGATION_ATTEMPTS,
    IAM_PROPAGATION_DELAY,
    create_service_account,
    delete_service_account,
    resolve_principal_member,
    service_account_absent,
)
from google.api_core import exceptions as gax
from google.api_core import retry as gax_retry
from google.cloud import compute_v1, iam_admin_v1, kms_v1, storage
from google.iam.v1 import iam_policy_pb2, options_pb2

# Role the run principal needs on tenant A's service account to impersonate it
# (mint short-lived tokens). Bound explicitly so the deny probes are
# self-contained rather than relying on an implicit project-level grant.
_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"
_TENANT_KMS_ROLE = "roles/cloudkms.viewer"
_TENANT_BUCKET_ROLE = "roles/storage.legacyBucketReader"
_TENANT_INSTANCE_ROLE = "roles/compute.instanceAdmin.v1"
_IAM_PROPAGATION_BUDGET_SECONDS = IAM_PROPAGATION_ATTEMPTS * IAM_PROPAGATION_DELAY
_OWN_STOP_BUDGET_SECONDS = 180
_OWN_STOP_POLL_SECONDS = 2.0


# Per-tenant resource name bases. The service-account local part is capped at 30
# chars, so the SA prefix is deliberately short (unique_suffix appends an 8-char
# run-id and a 4-char per-invocation discriminator is added below).
_SA_BASE_A = "isv-sec11-a"
_SA_BASE_B = "isv-sec11-b"
_VPC_BASE_A = "isv-sec11-a-vpc"
_VPC_BASE_B = "isv-sec11-b-vpc"
_SUBNET_BASE_A = "isv-sec11-a-subnet"
_SUBNET_BASE_B = "isv-sec11-b-subnet"
# Distinct, non-overlapping primary ranges per tenant (the instance NIC needs a
# subnet on a custom-mode VPC; the ranges are isolated config-plane evidence).
_SUBNET_CIDR_A = "10.94.0.0/24"
_SUBNET_CIDR_B = "10.95.0.0/24"
_KEY_BASE_A = "isv-sec11-a-key"
_KEY_BASE_B = "isv-sec11-b-key"
_BUCKET_BASE_A = "isv-sec11-a"
_BUCKET_BASE_B = "isv-sec11-b"
_INSTANCE_BASE_A = "isv-sec11-a-vm"
_INSTANCE_BASE_B = "isv-sec11-b-vm"

# Cloud KMS key rings cannot be deleted, so a single deterministic per-run ring
# holds both tenant keys; an AlreadyExists on a same-run re-run is benign.
_KEY_RING_BASE = "isv-sec11"

# A small public image / machine type is enough — these instances are never
# logged into, only used as the target identity of the cross-tenant deny probe.
_PROBE_MACHINE_TYPE = "e2-small"

# Token scope minted for the SA-A impersonation. cloud-platform lets the
# impersonated credential drive the KMS / Compute / Storage probe clients.
_IMPERSONATION_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

# The created-by marker stamped on every label-supporting resource so an
# operator ownership sweep can attribute orphans.
_CREATED_BY_LABEL = {CREATED_BY_LABEL: CREATED_BY_VALUE}
# Stable, collision-specific provenance used only for intentional same-run KMS
# reuse. Unlike ``created-by``, this proves the existing key belongs to this
# exact deterministic fixture name.
_FIXTURE_ID_LABEL = "isv-fixture"

# Display name stamped on every tenant service account.
_SA_DISPLAY_NAME = "ISV SEC11-01 tenant isolation fixture"

# A concurrent cleanup invocation using the same RUN_ID can
# delete tenant A's service account inside the grant/propagation window before its
# impersonation token is minted. The token is minted from a freshly created SA, so
# if the SA is swept mid-window it is re-created with a new name and the mint is
# retried, bounded to this many identity generations.
_MAX_SA_GENERATIONS = 3
# Preserve the original create exception for invocation-marker reconciliation;
# the storage client's default policy would otherwise retry into a final 409.
_NO_CREATE_RETRY = gax_retry.Retry(predicate=lambda _exc: False)


@dataclass
class Tenant:
    """Per-tenant fixture handles, populated during setup and used at teardown."""

    sa_base: str
    vpc_base: str
    subnet_base: str
    subnet_cidr: str
    key_base: str
    bucket_base: str
    instance_base: str
    sa_email: str = ""
    vpc_name: str = ""
    subnet_name: str = ""
    key_name: str = ""
    bucket_name: str = ""
    instance_name: str = ""
    zone: str = ""
    created: dict[str, bool] = field(default_factory=dict)
    owned_sa_emails: set[str] = field(default_factory=set)
    own_grants_added: set[str] = field(default_factory=set)
    token_creator_member: str = ""
    token_creator_grant_added: bool = False


def _sa_account_id(base: str) -> str:
    """Return a <=30-char SA local part: base + discriminator + run suffix."""
    # The discriminator prevents same-run retries from colliding, and placing
    # it before unique_suffix preserves the terminal run-id cleanup contract.
    return unique_suffix(f"{base}-{secrets.token_hex(2)}")


def _key_ring_path(project: str, location: str, ring_id: str) -> str:
    """Return the resource path of the shared per-run key ring (creating it idempotently)."""
    client = kms_v1.KeyManagementServiceClient()
    parent = f"projects/{project}/locations/{location}"
    ring_path = f"{parent}/keyRings/{ring_id}"
    try:
        client.create_key_ring(parent=parent, key_ring_id=ring_id, key_ring=kms_v1.KeyRing())
    except gax.AlreadyExists:
        pass
    return ring_path


def _create_crypto_key(
    ring_path: str,
    key_id: str,
    *,
    on_accepted: Callable[[], None] | None = None,
) -> tuple[str, bool]:
    """Return a verified symmetric CryptoKey and whether this call created it.

    Cloud KMS keys cannot be hard-deleted, so on a same-run re-run an
    AlreadyExists falls back to a verified get so the existing key is reused
    without transferring cleanup ownership to this invocation.
    """
    client = kms_v1.KeyManagementServiceClient()
    stable_labels = {**_CREATED_BY_LABEL, _FIXTURE_ID_LABEL: key_id}
    invocation_id = new_invocation_id()
    crypto_key = kms_v1.CryptoKey(
        purpose=kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT,
        labels=labels_with_invocation(stable_labels, invocation_id),
        version_template=kms_v1.CryptoKeyVersionTemplate(
            algorithm=kms_v1.CryptoKeyVersion.CryptoKeyVersionAlgorithm.GOOGLE_SYMMETRIC_ENCRYPTION,
        ),
    )
    key_name = f"{ring_path}/cryptoKeys/{key_id}"
    try:
        created = submit_owned_create(
            lambda: client.create_crypto_key(
                parent=ring_path,
                crypto_key_id=key_id,
                crypto_key=crypto_key,
                retry=_NO_CREATE_RETRY,
            ),
            lambda: client.get_crypto_key(name=key_name),
            lambda resource: has_invocation_label(resource, invocation_id),
            on_accepted=on_accepted,
        )
        return created.name, True
    except gax.AlreadyExists:
        # A definite conflict never transfers this invocation's ownership. The
        # permanently undeletable key may be reused only when its stable
        # fixture-id provenance and cryptographic shape exactly match.
        existing = client.get_crypto_key(name=key_name)
        expected_purpose = kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT
        expected_algorithm = kms_v1.CryptoKeyVersion.CryptoKeyVersionAlgorithm.GOOGLE_SYMMETRIC_ENCRYPTION
        actual_algorithm = getattr(getattr(existing, "version_template", None), "algorithm", None)
        if (
            existing.purpose != expected_purpose
            or actual_algorithm != expected_algorithm
            or any(
                dict(getattr(existing, "labels", None) or {}).get(label) != value
                for label, value in stable_labels.items()
            )
        ):
            raise RuntimeError(f"existing CryptoKey {existing.name} does not match the tenant fixture shape")
        return existing.name, False


def _create_bucket(
    client: storage.Client,
    project: str,
    location: str,
    bucket_name: str,
    *,
    on_accepted: Callable[[], None] | None = None,
) -> None:
    """Create a marked tenant bucket with ambiguous-ack ownership reconciliation."""
    invocation_id = new_invocation_id()
    bucket = client.bucket(bucket_name)
    bucket.labels = labels_with_invocation(_CREATED_BY_LABEL, invocation_id)
    bucket.iam_configuration.uniform_bucket_level_access_enabled = True
    submit_owned_create(
        lambda: client.create_bucket(
            bucket,
            project=project,
            location=location,
            retry=_NO_CREATE_RETRY,
        ),
        lambda: client.get_bucket(bucket_name),
        lambda resource: has_invocation_label(resource, invocation_id),
        on_accepted=on_accepted,
    )


def _create_tenant_sa(project: str, tenant: Tenant) -> None:
    """Create the tenant's cross-tenant probe identity (its service account).

    Created last in the provisioning order (after the heavy VPC/instance fixtures)
    and immediately before impersonation so the window in which a concurrent owner
    sweep can delete an in-use SA is as small as possible.
    """
    account_id = _sa_account_id(tenant.sa_base)
    candidate_email = f"{account_id}@{project}.iam.gserviceaccount.com"
    tenant.sa_email = candidate_email
    tenant.token_creator_member = ""
    tenant.token_creator_grant_added = False

    def _record_acceptance() -> None:
        tenant.created["sa"] = True
        tenant.owned_sa_emails.add(candidate_email)

    tenant.sa_email = create_service_account(
        project,
        account_id,
        display_name=_SA_DISPLAY_NAME,
        description=CREATED_BY_DESCRIPTION,
        on_accepted=_record_acceptance,
    )


def _provision_tenant_resources(
    *,
    project: str,
    location: str,
    ring_path: str,
    storage_client: storage.Client,
    tenant: Tenant,
) -> None:
    """Provision the tenant's VPC/subnet/KMS-key/bucket/instance fixtures.

    The service account is created separately (``_create_tenant_sa``) AFTER these
    fixtures so its sweep-exposed lifetime overlaps only the brief impersonation
    step. Each created flag is stamped after synchronous API acceptance and
    before any async wait, so conflicts preserve foreign resources while wait
    failures still hand teardown the ownership signal.
    """
    # VPC + subnet: insert_network creates a custom-mode network (no auto
    # subnets) and stamps the description ownership marker (the Network proto
    # carries no labels field). A custom-mode NIC needs an explicit regional
    # subnetwork, so each tenant gets its own subnet on a distinct primary range.
    region = zone_to_region(tenant.zone)
    tenant.vpc_name = unique_suffix(tenant.vpc_base)
    insert_network(
        project,
        tenant.vpc_name,
        on_accepted=lambda: tenant.created.update(vpc=True),
    )

    tenant.subnet_name = unique_suffix(tenant.subnet_base)
    insert_subnetwork(
        project,
        region,
        tenant.subnet_name,
        tenant.vpc_name,
        tenant.subnet_cidr,
        on_accepted=lambda: tenant.created.update(subnet=True),
    )

    # Cloud KMS CryptoKey in the shared per-run ring.
    key_id = unique_suffix(tenant.key_base)
    tenant.key_name = f"{ring_path}/cryptoKeys/{key_id}"
    tenant.created["kms_key"] = False
    _returned_key_name, _created = _create_crypto_key(
        ring_path,
        key_id,
        on_accepted=lambda: tenant.created.update(kms_key=True),
    )
    tenant.key_name = _returned_key_name

    # Cloud Storage bucket.
    tenant.bucket_name = unique_suffix(tenant.bucket_base)
    _create_bucket(
        storage_client,
        project,
        location,
        tenant.bucket_name,
        on_accepted=lambda: tenant.created.update(bucket=True),
    )

    # Compute instance on the tenant's own subnet, no external IP — it is never
    # logged into, only used as the target identity of the cross-tenant probe.
    tenant.instance_name = unique_suffix(tenant.instance_base)
    instance = build_probe_instance(
        project=project,
        zone=tenant.zone,
        name=tenant.instance_name,
        network_name=tenant.vpc_name,
        subnet_name=tenant.subnet_name,
        machine_type=_PROBE_MACHINE_TYPE,
        external_ip=False,
    )
    # Stamp the ownership label so the teardown safety-net sweep can reclaim a
    # leaked probe VM after a hard crash (the Instance proto supports labels; the
    # same-step finally block is the primary cleanup path).
    instance.labels = dict(_CREATED_BY_LABEL)
    insert_instance(
        project,
        tenant.zone,
        instance,
        on_accepted=lambda: tenant.created.update(instance=True),
    )


def _delete_compute_idempotent(
    delete_fn: Callable[..., Any],
    get_fn: Callable[..., Any],
    *args: Any,
    resource_desc: str,
) -> str | None:
    """Delete an owned Compute resource, folding an already-gone resource into success.

    ``delete_fn`` (``delete_instance`` / ``delete_subnetwork`` / ``delete_network``)
    catches a synchronous ``NotFound``, but a concurrent cleanup invocation using
    the same RUN_ID can delete the resource so its delete-op
    completes with ``RESOURCE_NOT_FOUND`` — re-raised as a ``RuntimeError`` by the
    op-waiter, which ``delete_with_retry`` then surfaces as a generic failure
    (``False``). The resource is owned by this run, so confirm absence with a
    ``get`` (same positional args): a ``NotFound`` means it is genuinely gone
    (success); anything else is a real teardown failure. Returns an error string
    only on a genuine failure.
    """
    if delete_with_retry(delete_fn, *args, resource_desc=resource_desc):
        return None
    try:
        get_fn(*args)
    except gax.NotFound:
        return None  # already gone (a concurrent owner sweep removed it) -> success
    except Exception:  # get failed for another reason; report the delete failure
        pass
    return f"delete {resource_desc}"


def _teardown_tenant(*, project: str, tenant: Tenant) -> list[str]:
    """Best-effort teardown of every resource the setup created for ``tenant``."""
    errors: list[str] = []

    # Remove temporary access before deleting its resource. This is essential
    # for undeletable KMS keys and makes cleanup exact even when a later delete
    # fails. Only bindings this invocation proved it added are touched.
    errors.extend(_remove_tenant_own_access(project, tenant))
    if tenant.token_creator_grant_added and tenant.token_creator_member and tenant.sa_email:
        try:
            _remove_token_creator(tenant.sa_email, tenant.token_creator_member)
            tenant.token_creator_grant_added = False
        except gax.NotFound:
            tenant.token_creator_grant_added = False
        except Exception as exc:
            errors.append(f"remove token-creator grant from {tenant.sa_email}: {exc}")

    if tenant.created.get("instance") and tenant.instance_name and tenant.zone:
        err = _delete_compute_idempotent(
            delete_instance,
            get_instance,
            project,
            tenant.zone,
            tenant.instance_name,
            resource_desc=f"instance {tenant.instance_name}",
        )
        if err:
            errors.append(err)

    # Subnet must be deleted before its parent network (dependency order); the
    # instance above is its only dependent.
    if tenant.created.get("subnet") and tenant.subnet_name and tenant.zone:
        region = zone_to_region(tenant.zone)
        err = _delete_compute_idempotent(
            delete_subnetwork,
            get_subnetwork,
            project,
            region,
            tenant.subnet_name,
            resource_desc=f"subnetwork {tenant.subnet_name}",
        )
        if err:
            errors.append(err)

    if tenant.created.get("vpc") and tenant.vpc_name:
        err = _delete_compute_idempotent(
            delete_network,
            get_network,
            project,
            tenant.vpc_name,
            resource_desc=f"network {tenant.vpc_name}",
        )
        if err:
            errors.append(err)

    if tenant.created.get("bucket") and tenant.bucket_name:
        try:
            storage.Client(project=project).bucket(tenant.bucket_name).delete(force=True)
        except gax.NotFound:
            pass
        except Exception as e:  # best-effort sweep: never let one delete abort the rest
            errors.append(f"delete bucket {tenant.bucket_name}: {e}")

    # Cloud KMS keys cannot be hard-deleted; schedule the primary version for
    # destruction so the key material stops being usable. AlreadyExists on a
    # re-run leaves a pre-destroyed version, which is benign.
    if tenant.created.get("kms_key") and tenant.key_name:
        try:
            kms_v1.KeyManagementServiceClient().destroy_crypto_key_version(
                name=f"{tenant.key_name}/cryptoKeyVersions/1"
            )
        except gax.FailedPrecondition:
            # Version already scheduled/destroyed on a prior same-run pass.
            pass
        except gax.NotFound:
            pass
        except Exception as e:  # best-effort sweep: never let one delete abort the rest
            errors.append(f"destroy key version for {tenant.key_name}: {e}")

    for sa_email in sorted(tenant.owned_sa_emails):
        if not delete_service_account(sa_email, project=project):
            errors.append(f"delete service account {sa_email}")

    return errors


def _grant_token_creator(sa_email: str, member: str, *, on_accepted: Callable[[], None]) -> None:
    """Grant ``member`` ``serviceAccountTokenCreator`` on ``sa_email`` so it can be impersonated."""
    iam = iam_admin_v1.IAMClient()
    resource = f"projects/-/serviceAccounts/{sa_email}"

    def _read() -> Any:
        return iam.get_iam_policy(request=iam_policy_pb2.GetIamPolicyRequest(resource=resource))

    def _write(policy: Any) -> Any:
        return iam.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy))

    def _ensure(policy: Any) -> bool:
        return ensure_unconditional_binding_member(policy, _TOKEN_CREATOR_ROLE, member)

    modify_iam_policy_with_retry(
        _read,
        _write,
        _ensure,
        resource_desc=f"service account {sa_email}",
        on_change_accepted=on_accepted,
    )


def _remove_token_creator(sa_email: str, member: str) -> None:
    """Remove only this run principal's unconditional token-creator grant."""
    iam = iam_admin_v1.IAMClient()
    resource = f"projects/-/serviceAccounts/{sa_email}"

    def _read() -> Any:
        return iam.get_iam_policy(request=iam_policy_pb2.GetIamPolicyRequest(resource=resource))

    def _write(policy: Any) -> Any:
        return iam.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy))

    def _remove(policy: Any) -> bool:
        return bool(
            remove_binding_members(
                policy,
                binding_matches=lambda binding: (
                    binding.role == _TOKEN_CREATOR_ROLE and not _binding_is_conditioned(binding)
                ),
                member_matches=lambda candidate: candidate == member,
            )
        )

    modify_iam_policy_with_retry(_read, _write, _remove, resource_desc=f"service account {sa_email}")


def _binding_is_conditioned(binding: Any) -> bool:
    """Return True when an IAM binding carries any condition field."""
    condition = getattr(binding, "condition", None)
    if isinstance(binding, dict):
        condition = binding.get("condition")
    if isinstance(condition, dict):
        return any(condition.get(field) for field in ("title", "description", "expression"))
    return bool(
        getattr(condition, "title", "") or getattr(condition, "description", "") or getattr(condition, "expression", "")
    )


def _ensure_kms_own_access(tenant: Tenant, *, on_accepted: Callable[[], None]) -> None:
    """Grant the tenant identity read access on exactly its own CryptoKey."""
    client = kms_v1.KeyManagementServiceClient()
    member = f"serviceAccount:{tenant.sa_email}"

    def _read() -> Any:
        return client.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(
                resource=tenant.key_name,
                options=options_pb2.GetPolicyOptions(requested_policy_version=3),
            )
        )

    def _write(policy: Any) -> Any:
        policy.version = 3
        return client.set_iam_policy(
            request=iam_policy_pb2.SetIamPolicyRequest(resource=tenant.key_name, policy=policy)
        )

    def _ensure(policy: Any) -> bool:
        return ensure_unconditional_binding_member(policy, _TENANT_KMS_ROLE, member)

    modify_iam_policy_with_retry(
        _read,
        _write,
        _ensure,
        resource_desc=f"CryptoKey {tenant.key_name}",
        on_change_accepted=on_accepted,
    )


def _remove_kms_own_access(tenant: Tenant) -> None:
    """Remove the tenant identity's exact own-key viewer grant."""
    client = kms_v1.KeyManagementServiceClient()
    member = f"serviceAccount:{tenant.sa_email}"

    def _read() -> Any:
        return client.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(
                resource=tenant.key_name,
                options=options_pb2.GetPolicyOptions(requested_policy_version=3),
            )
        )

    def _write(policy: Any) -> Any:
        policy.version = 3
        return client.set_iam_policy(
            request=iam_policy_pb2.SetIamPolicyRequest(resource=tenant.key_name, policy=policy)
        )

    def _remove(policy: Any) -> bool:
        return bool(
            remove_binding_members(
                policy,
                binding_matches=lambda binding: (
                    binding.role == _TENANT_KMS_ROLE and not _binding_is_conditioned(binding)
                ),
                member_matches=lambda candidate: candidate == member,
            )
        )

    modify_iam_policy_with_retry(_read, _write, _remove, resource_desc=f"CryptoKey {tenant.key_name}")


def _ensure_storage_binding_member(policy: Any, role: str, member: str) -> bool:
    """Ensure one unconditional member in a Cloud Storage IAM policy."""
    for binding in policy.bindings:
        if binding.get("role") != role or _binding_is_conditioned(binding):
            continue
        members: set[str] = set(binding.get("members") or ())
        if member in members:
            return False
        members.add(member)
        binding["members"] = members
        return True
    policy.bindings = [*policy.bindings, {"role": role, "members": {member}}]
    return True


def _remove_storage_binding_member(policy: Any, role: str, member: str) -> bool:
    """Remove one exact member while preserving foreign Storage bindings."""
    changed = False
    retained: list[dict[str, Any]] = []
    for binding in policy.bindings:
        clone = dict(binding)
        members: set[str] = set(binding.get("members") or ())
        if binding.get("role") == role and not _binding_is_conditioned(binding) and member in members:
            members.remove(member)
            changed = True
        if members:
            clone["members"] = members
            retained.append(clone)
    if changed:
        policy.bindings = retained
    return changed


def _ensure_bucket_own_access(project: str, tenant: Tenant, *, on_accepted: Callable[[], None]) -> None:
    """Grant the tenant identity metadata read on exactly its own bucket."""
    bucket = storage.Client(project=project).bucket(tenant.bucket_name)
    member = f"serviceAccount:{tenant.sa_email}"

    def _read() -> Any:
        return bucket.get_iam_policy(requested_policy_version=3)

    def _write(policy: Any) -> Any:
        return bucket.set_iam_policy(policy)

    def _ensure(policy: Any) -> bool:
        return _ensure_storage_binding_member(policy, _TENANT_BUCKET_ROLE, member)

    modify_iam_policy_with_retry(
        _read,
        _write,
        _ensure,
        resource_desc=f"bucket {tenant.bucket_name}",
        on_change_accepted=on_accepted,
    )


def _remove_bucket_own_access(project: str, tenant: Tenant) -> None:
    """Remove the tenant identity's exact own-bucket reader grant."""
    bucket = storage.Client(project=project).bucket(tenant.bucket_name)
    member = f"serviceAccount:{tenant.sa_email}"
    modify_iam_policy_with_retry(
        lambda: bucket.get_iam_policy(requested_policy_version=3),
        bucket.set_iam_policy,
        lambda policy: _remove_storage_binding_member(policy, _TENANT_BUCKET_ROLE, member),
        resource_desc=f"bucket {tenant.bucket_name}",
    )


def _ensure_compute_binding_member(policy: Any, role: str, member: str) -> bool:
    """Ensure one unconditional member in a Compute resource IAM policy."""
    for binding in policy.bindings:
        if binding.role != role or _binding_is_conditioned(binding):
            continue
        if member in binding.members:
            return False
        binding.members.append(member)
        return True
    policy.bindings.append(compute_v1.Binding(role=role, members=[member]))
    return True


def _remove_compute_binding_member(policy: Any, role: str, member: str) -> bool:
    """Remove one exact member while preserving foreign Compute bindings."""
    changed = False
    retained: list[Any] = []
    for binding in policy.bindings:
        clone_fields: dict[str, Any] = {
            "role": binding.role,
            "members": list(binding.members),
        }
        condition = getattr(binding, "condition", None)
        if condition:
            clone_fields["condition"] = condition
        clone = compute_v1.Binding(**clone_fields)
        if binding.role == role and not _binding_is_conditioned(binding) and member in binding.members:
            clone.members = [candidate for candidate in binding.members if candidate != member]
            changed = True
        if clone.members:
            retained.append(clone)
    if changed:
        policy.bindings = retained
    return changed


def _ensure_instance_own_access(project: str, tenant: Tenant, *, on_accepted: Callable[[], None]) -> None:
    """Grant the tenant identity admin access on exactly its own VM."""
    client = compute_v1.InstancesClient()
    member = f"serviceAccount:{tenant.sa_email}"

    def _read() -> Any:
        return client.get_iam_policy(project=project, zone=tenant.zone, resource=tenant.instance_name)

    def _write(policy: Any) -> Any:
        return client.set_iam_policy(
            project=project,
            zone=tenant.zone,
            resource=tenant.instance_name,
            zone_set_policy_request_resource=compute_v1.ZoneSetPolicyRequest(policy=policy),
        )

    def _ensure(policy: Any) -> bool:
        return _ensure_compute_binding_member(policy, _TENANT_INSTANCE_ROLE, member)

    modify_iam_policy_with_retry(
        _read,
        _write,
        _ensure,
        resource_desc=f"instance {tenant.instance_name}",
        on_change_accepted=on_accepted,
    )


def _remove_instance_own_access(project: str, tenant: Tenant) -> None:
    """Remove the tenant identity's exact own-instance admin grant."""
    client = compute_v1.InstancesClient()
    member = f"serviceAccount:{tenant.sa_email}"

    def _write(policy: Any) -> Any:
        return client.set_iam_policy(
            project=project,
            zone=tenant.zone,
            resource=tenant.instance_name,
            zone_set_policy_request_resource=compute_v1.ZoneSetPolicyRequest(policy=policy),
        )

    modify_iam_policy_with_retry(
        lambda: client.get_iam_policy(project=project, zone=tenant.zone, resource=tenant.instance_name),
        _write,
        lambda policy: _remove_compute_binding_member(policy, _TENANT_INSTANCE_ROLE, member),
        resource_desc=f"instance {tenant.instance_name}",
    )


def _grant_tenant_own_access(project: str, tenant: Tenant) -> None:
    """Install and track the three exact own-resource grants for one tenant."""
    _ensure_kms_own_access(tenant, on_accepted=lambda: tenant.own_grants_added.add("kms"))
    _ensure_bucket_own_access(project, tenant, on_accepted=lambda: tenant.own_grants_added.add("bucket"))
    _ensure_instance_own_access(project, tenant, on_accepted=lambda: tenant.own_grants_added.add("instance"))


def _remove_tenant_own_access(project: str, tenant: Tenant) -> list[str]:
    """Best-effort exact removal of every own-resource grant this run added."""
    errors: list[str] = []
    removers = {
        "kms": lambda: _remove_kms_own_access(tenant),
        "bucket": lambda: _remove_bucket_own_access(project, tenant),
        "instance": lambda: _remove_instance_own_access(project, tenant),
    }
    for grant_name in sorted(tenant.own_grants_added):
        try:
            removers[grant_name]()
            tenant.own_grants_added.discard(grant_name)
        except gax.NotFound:
            tenant.own_grants_added.discard(grant_name)
        except Exception as exc:
            errors.append(f"remove {grant_name} own-access grant for {tenant.sa_email}: {exc}")
    return errors


def _build_tenant_credentials(source_credentials: Any, sa_email: str) -> Any:
    """Return impersonated credentials for one tenant service account.

    The run principal is granted ``roles/iam.serviceAccountTokenCreator`` on the
    self-created SA before this call, so it can impersonate it to drive the
    cross-tenant deny probes from tenant A's identity.
    """
    return google.auth.impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=sa_email,
        target_scopes=list(_IMPERSONATION_SCOPES),
    )


def _is_denied(exc: Exception) -> bool:
    """Return True iff ``exc`` represents an authorization deny (403)."""
    return isinstance(exc, gax.PermissionDenied | gax.Forbidden)


def _cross_tenant_route(project: str, vpc_name: str, other_cidr: str) -> str | None:
    """Return an error string iff ``vpc_name`` has a custom route toward ``other_cidr``.

    GCE auto-routes (the ``0.0.0.0/0`` default plus the per-subnet local routes)
    are skipped: they are never an operator-wired cross-tenant path, and the
    default route's ``0.0.0.0/0`` overlaps every CIDR, which would fake a
    cross-tenant signal. Matching the other tenant's exact primary range mirrors
    the AWS oracle's ``DestinationCidrBlock in tenant_b_cidrs`` route check.
    """
    for route in list_routes_for_network(project, vpc_name):
        if is_auto_route(route):
            continue
        if route.dest_range == other_cidr:
            return f"VPC {vpc_name} carries route {route.name} toward tenant range {other_cidr}"
    return None


def _probe_network_isolation(project: str, tenant_a: Tenant, tenant_b: Tenant) -> dict[str, Any]:
    """Verify neither tenant VPC peers with or routes toward the other (config-plane inspection).

    Two custom-mode VPCs in one project are mutually unreachable unless a peering
    or an explicit cross-tenant route is wired in. Mirrors the AWS orchestrator
    no-peering / no-shared-route check: a peering naming the other tenant's
    network on either side, OR a custom route whose destination is the other
    tenant's primary range, fails the probe.
    """
    a_peers = network_peerings(project, tenant_a.vpc_name)
    b_peers = network_peerings(project, tenant_b.vpc_name)

    for peer in a_peers:
        if short_name(getattr(peer, "network", "")) == tenant_b.vpc_name:
            return {
                "passed": False,
                "error": f"VPC {tenant_a.vpc_name} peers with tenant B VPC {tenant_b.vpc_name}",
            }
    for peer in b_peers:
        if short_name(getattr(peer, "network", "")) == tenant_a.vpc_name:
            return {
                "passed": False,
                "error": f"VPC {tenant_b.vpc_name} peers with tenant A VPC {tenant_a.vpc_name}",
            }

    route_conflict = _cross_tenant_route(project, tenant_a.vpc_name, tenant_b.subnet_cidr) or _cross_tenant_route(
        project, tenant_b.vpc_name, tenant_a.subnet_cidr
    )
    if route_conflict:
        return {"passed": False, "error": route_conflict}

    return {
        "passed": True,
        "message": (
            f"No cross-tenant peering or route between tenant A VPC {tenant_a.vpc_name} "
            f"and tenant B VPC {tenant_b.vpc_name}"
        ),
    }


def _shared_propagation_deadline(propagation_deadline: float | None) -> float:
    """Return the caller's shared deadline or start one bounded budget."""
    if propagation_deadline is not None:
        return propagation_deadline
    return time.monotonic() + _IAM_PROPAGATION_BUDGET_SECONDS


def _wait_for_propagation(*, attempt: int, deadline: float) -> bool:
    """Sleep for one retry only while both the attempt and shared budgets allow."""
    if attempt >= IAM_PROPAGATION_ATTEMPTS:
        return False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(IAM_PROPAGATION_DELAY, remaining))
    return True


def _positive_access_probe(
    name: str,
    call: Callable[[], Any],
    *,
    propagation_deadline: float | None = None,
) -> dict[str, Any]:
    """Require an own-resource call to succeed, allowing IAM propagation time."""
    probe: dict[str, Any] = {"name": name}
    deadline = _shared_propagation_deadline(propagation_deadline)
    for attempt in range(1, IAM_PROPAGATION_ATTEMPTS + 1):
        try:
            call()
        except (gax.PermissionDenied, gax.Forbidden, gax.NotFound) as exc:
            if _wait_for_propagation(attempt=attempt, deadline=deadline):
                continue
            probe.update(passed=False, code=type(exc).__name__, error=str(exc))
        except Exception as exc:
            probe.update(passed=False, code=type(exc).__name__, error=str(exc))
        else:
            probe["passed"] = True
        return probe
    raise AssertionError("positive access retry loop exhausted without a verdict")


def _denied_probe(name: str, call: Callable[[], Any], *, accept_not_found: bool = False) -> dict[str, Any]:
    """Require one cross-tenant call to fail specifically as authorization deny."""
    probe: dict[str, Any] = {"name": name}
    try:
        call()
    except Exception as exc:
        probe["passed"] = _is_denied(exc) or (accept_not_found and _looks_like_404(exc))
        probe["code"] = type(exc).__name__
        if not probe["passed"]:
            probe["error"] = str(exc)
    else:
        probe["passed"] = False
        probe["error"] = f"{name} unexpectedly succeeded"
    return probe


def _probe_data_isolation(
    credentials_a: Any,
    credentials_b: Any,
    tenant_a: Tenant,
    tenant_b: Tenant,
    *,
    propagation_deadline: float | None = None,
) -> dict[str, Any]:
    """Require own-key access and symmetric cross-key denials."""
    client_a = kms_v1.KeyManagementServiceClient(credentials=credentials_a)
    client_b = kms_v1.KeyManagementServiceClient(credentials=credentials_b)
    probes = [
        _positive_access_probe(
            "tenant_a_kms_own_access_allowed",
            lambda: client_a.get_crypto_key(name=tenant_a.key_name),
            propagation_deadline=propagation_deadline,
        ),
        _positive_access_probe(
            "tenant_b_kms_own_access_allowed",
            lambda: client_b.get_crypto_key(name=tenant_b.key_name),
            propagation_deadline=propagation_deadline,
        ),
        _denied_probe(
            "tenant_a_to_b_kms_access_denied",
            lambda: client_a.get_crypto_key(name=tenant_b.key_name),
        ),
        _denied_probe(
            "tenant_b_to_a_kms_access_denied",
            lambda: client_b.get_crypto_key(name=tenant_a.key_name),
        ),
    ]
    return {"passed": all(probe["passed"] for probe in probes), "probes": probes}


def _submit_own_stop(
    client: compute_v1.InstancesClient,
    project: str,
    tenant: Tenant,
) -> None:
    """Submit a stop for the disposable own VM using the tenant identity.

    GCP's instance ``testIamPermissions`` endpoint requires project-level
    ``compute.instances.list`` merely to invoke it. Granting that permission to
    a resource-scoped tenant would broaden this fixture, so the positive control
    performs the real operation instead.
    """
    client.stop(project=project, zone=tenant.zone, instance=tenant.instance_name)


def _wait_for_own_stops(
    project: str,
    pending: list[tuple[compute_v1.InstancesClient, Tenant, dict[str, Any]]],
) -> None:
    """Observe every accepted own stop concurrently under one total budget."""
    waiting = [(client, tenant, probe) for client, tenant, probe in pending if probe["passed"]]
    deadline = time.monotonic() + _OWN_STOP_BUDGET_SECONDS
    while waiting:
        still_waiting: list[tuple[compute_v1.InstancesClient, Tenant, dict[str, Any]]] = []
        for client, tenant, probe in waiting:
            try:
                instance = client.get(project=project, zone=tenant.zone, instance=tenant.instance_name)
            except Exception as exc:
                probe.update(passed=False, code=type(exc).__name__, error=str(exc))
                continue
            if str(getattr(instance, "status", "")).upper() != "TERMINATED":
                still_waiting.append((client, tenant, probe))
        if not still_waiting:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for _client, tenant, probe in still_waiting:
                probe.update(
                    passed=False,
                    code="TimeoutError",
                    error=f"own instance {tenant.instance_name} did not reach TERMINATED",
                )
            return
        time.sleep(min(_OWN_STOP_POLL_SECONDS, remaining))
        waiting = still_waiting


def _probe_compute_isolation(
    credentials_a: Any,
    credentials_b: Any,
    project: str,
    tenant_a: Tenant,
    tenant_b: Tenant,
    *,
    propagation_deadline: float | None = None,
) -> dict[str, Any]:
    """Require own-VM reads and symmetric cross-VM get/stop denials."""
    client_a = compute_v1.InstancesClient(credentials=credentials_a)
    client_b = compute_v1.InstancesClient(credentials=credentials_b)
    probes = [
        _positive_access_probe(
            "tenant_a_compute_own_access_allowed",
            lambda: client_a.get(project=project, zone=tenant_a.zone, instance=tenant_a.instance_name),
            propagation_deadline=propagation_deadline,
        ),
        _positive_access_probe(
            "tenant_b_compute_own_access_allowed",
            lambda: client_b.get(project=project, zone=tenant_b.zone, instance=tenant_b.instance_name),
            propagation_deadline=propagation_deadline,
        ),
    ]
    stop_probes = [
        _positive_access_probe(
            "tenant_a_compute_own_stop_allowed",
            lambda: _submit_own_stop(client_a, project, tenant_a),
            propagation_deadline=propagation_deadline,
        ),
        _positive_access_probe(
            "tenant_b_compute_own_stop_allowed",
            lambda: _submit_own_stop(client_b, project, tenant_b),
            propagation_deadline=propagation_deadline,
        ),
    ]
    probes.extend(stop_probes)
    _wait_for_own_stops(
        project,
        [
            (client_a, tenant_a, stop_probes[0]),
            (client_b, tenant_b, stop_probes[1]),
        ],
    )
    probes.extend(
        [
            _denied_probe(
                "tenant_a_to_b_instances_get_denied",
                lambda: client_a.get(project=project, zone=tenant_b.zone, instance=tenant_b.instance_name),
            ),
            _denied_probe(
                "tenant_a_to_b_instances_stop_denied",
                lambda: client_a.stop(project=project, zone=tenant_b.zone, instance=tenant_b.instance_name),
            ),
            _denied_probe(
                "tenant_b_to_a_instances_get_denied",
                lambda: client_b.get(project=project, zone=tenant_a.zone, instance=tenant_a.instance_name),
            ),
            _denied_probe(
                "tenant_b_to_a_instances_stop_denied",
                lambda: client_b.stop(project=project, zone=tenant_a.zone, instance=tenant_a.instance_name),
            ),
        ]
    )
    return {"passed": all(probe["passed"] for probe in probes), "probes": probes}


def _probe_storage_isolation(
    credentials_a: Any,
    credentials_b: Any,
    project: str,
    tenant_a: Tenant,
    tenant_b: Tenant,
    *,
    propagation_deadline: float | None = None,
) -> dict[str, Any]:
    """Require own-bucket reads and symmetric cross-bucket denials."""
    client_a = storage.Client(project=project, credentials=credentials_a)
    client_b = storage.Client(project=project, credentials=credentials_b)
    probes = [
        _positive_access_probe(
            "tenant_a_storage_own_access_allowed",
            lambda: client_a.get_bucket(tenant_a.bucket_name),
            propagation_deadline=propagation_deadline,
        ),
        _positive_access_probe(
            "tenant_b_storage_own_access_allowed",
            lambda: client_b.get_bucket(tenant_b.bucket_name),
            propagation_deadline=propagation_deadline,
        ),
        _denied_probe(
            "tenant_a_to_b_storage_access_denied",
            lambda: client_a.get_bucket(tenant_b.bucket_name),
            accept_not_found=True,
        ),
        _denied_probe(
            "tenant_b_to_a_storage_access_denied",
            lambda: client_b.get_bucket(tenant_a.bucket_name),
            accept_not_found=True,
        ),
    ]
    return {"passed": all(probe["passed"] for probe in probes), "probes": probes}


def _looks_like_404(exc: Exception) -> bool:
    """Return True iff ``exc`` carries a 404 status (Cloud Storage existence-hiding)."""
    code = getattr(exc, "code", None)
    if code == 404:
        return True
    return "404" in str(exc)


def _skipped_result(reason: str) -> dict[str, Any]:
    """Return a structured top-level skip payload (validator skips rather than fabricating a pass)."""
    return {
        "success": True,
        "platform": "security",
        "test_name": "tenant_isolation_test",
        "skipped": True,
        "skip_reason": reason,
        "tenant_a_id": "",
        "tenant_b_id": "",
        "sa_created": False,
        "vpc_created": False,
        "kms_key_created": False,
        "bucket_created": False,
        "instance_created": False,
        "tests": {
            "network_isolated": {"passed": False},
            "data_isolated": {"passed": False},
            "compute_isolated": {"passed": False},
            "storage_isolated": {"passed": False},
        },
    }


# Setup-permission denials that cannot provision the fixture surface as a
# structured skip rather than a SEC11-01 failure (the validator honors it).
_SKIPPABLE_SETUP_ERRORS: tuple[type[Exception], ...] = (gax.PermissionDenied, gax.Forbidden)


@handle_gcp_errors
def main() -> int:
    """Provision two tenants, run cross-tenant deny probes, emit JSON, clean up."""
    parser = argparse.ArgumentParser(description="Tenant isolation test (SEC11-01)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve run-owned fixtures for later teardown")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "tenant_isolation_test",
        "tenant_a_id": "",
        "tenant_b_id": "",
        "sa_created": False,
        "vpc_created": False,
        "kms_key_created": False,
        "bucket_created": False,
        "instance_created": False,
        "tests": {
            "network_isolated": {"passed": False},
            "data_isolated": {"passed": False},
            "compute_isolated": {"passed": False},
            "storage_isolated": {"passed": False},
        },
    }

    project = ""
    skip_payload: dict[str, Any] | None = None
    tenant_a = Tenant(
        _SA_BASE_A, _VPC_BASE_A, _SUBNET_BASE_A, _SUBNET_CIDR_A, _KEY_BASE_A, _BUCKET_BASE_A, _INSTANCE_BASE_A
    )
    tenant_b = Tenant(
        _SA_BASE_B, _VPC_BASE_B, _SUBNET_BASE_B, _SUBNET_CIDR_B, _KEY_BASE_B, _BUCKET_BASE_B, _INSTANCE_BASE_B
    )

    try:
        project = resolve_project(args.project)
        location = args.region or "us-central1"
        zone = narrow_region_to_zone(args.region) if args.region else narrow_region_to_zone("us-central1")
        tenant_a.zone = zone
        tenant_b.zone = zone

        source_credentials, _ = google.auth.default(scopes=list(_IMPERSONATION_SCOPES))
        storage_client = storage.Client(project=project, credentials=source_credentials)

        try:
            ring_path = _key_ring_path(project, location, unique_suffix(_KEY_RING_BASE))
            # Provision the heavy fixtures (VPC/subnet/KMS/bucket/instance) FIRST so
            # each tenant's probe targets exist, then create the service accounts
            # LAST — just before they are impersonated — to minimize the window in
            # which a concurrent cleanup invocation using the same RUN_ID can
            # delete an in-use SA.
            _provision_tenant_resources(
                project=project,
                location=location,
                ring_path=ring_path,
                storage_client=storage_client,
                tenant=tenant_a,
            )
            _provision_tenant_resources(
                project=project,
                location=location,
                ring_path=ring_path,
                storage_client=storage_client,
                tenant=tenant_b,
            )
            _create_tenant_sa(project, tenant_a)
            _create_tenant_sa(project, tenant_b)
        except _SKIPPABLE_SETUP_ERRORS as exc:
            partial = any(t.created for t in (tenant_a, tenant_b))
            if not partial:
                skip_payload = _skipped_result(
                    f"cannot provision SEC11-01 tenant fixture: {exc}; the run credential needs "
                    "service-account / VPC / Cloud KMS / Cloud Storage / Compute create+delete permissions"
                )
            else:
                raise

        # Roll the per-tenant created flags up into the contract ownership flags
        # before the probes so a probe-phase crash still hands teardown the
        # signal. Aggregate: a flag is true when either tenant created it.
        result["sa_created"] = any(t.created.get("sa") for t in (tenant_a, tenant_b))
        result["vpc_created"] = any(t.created.get("vpc") for t in (tenant_a, tenant_b))
        result["kms_key_created"] = any(t.created.get("kms_key") for t in (tenant_a, tenant_b))
        result["bucket_created"] = any(t.created.get("bucket") for t in (tenant_a, tenant_b))
        result["instance_created"] = any(t.created.get("instance") for t in (tenant_a, tenant_b))

        if skip_payload is None:
            # Mint each tenant's identity first, then install resource-level
            # grants only on its own key/bucket/VM. Positive controls prove those
            # grants work before symmetric cross-tenant denials can pass.
            operator_member = resolve_principal_member()
            propagation_deadline = time.monotonic() + _IAM_PROPAGATION_BUDGET_SECONDS
            credentials_a = _establish_impersonation(
                source_credentials,
                project,
                tenant_a,
                operator_member,
                propagation_deadline=propagation_deadline,
            )
            credentials_b = _establish_impersonation(
                source_credentials,
                project,
                tenant_b,
                operator_member,
                propagation_deadline=propagation_deadline,
            )
            _grant_tenant_own_access(project, tenant_a)
            _grant_tenant_own_access(project, tenant_b)
            result["tenant_a_id"] = tenant_a.sa_email
            result["tenant_b_id"] = tenant_b.sa_email

            result["tests"]["network_isolated"] = _probe_network_isolation(project, tenant_a, tenant_b)
            result["tests"]["data_isolated"] = _probe_data_isolation(
                credentials_a,
                credentials_b,
                tenant_a,
                tenant_b,
                propagation_deadline=propagation_deadline,
            )
            result["tests"]["compute_isolated"] = _probe_compute_isolation(
                credentials_a,
                credentials_b,
                project,
                tenant_a,
                tenant_b,
                propagation_deadline=propagation_deadline,
            )
            result["tests"]["storage_isolated"] = _probe_storage_isolation(
                credentials_a,
                credentials_b,
                project,
                tenant_a,
                tenant_b,
                propagation_deadline=propagation_deadline,
            )
            result["success"] = all(t.get("passed") for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        result["success"] = False
    finally:
        cleanup_errors: list[str] = []
        if args.skip_destroy:
            result["cleanup_skipped"] = True
        else:
            for tenant in (tenant_a, tenant_b):
                cleanup_errors.extend(_teardown_tenant(project=project, tenant=tenant))
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors

    # A structured skip wins only when nothing leaked, so the validator skips
    # rather than evaluating an empty result.
    if skip_payload is not None and not result.get("cleanup_errors"):
        print(json.dumps(skip_payload, indent=2))
        return 0

    preserve_success_after_cleanup(result)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


def _missing_account_signal(exc: Exception) -> bool:
    """Return whether ``exc`` can represent a missing service account."""
    if isinstance(exc, gax.NotFound):
        return True
    msg = str(exc).lower()
    return "account deleted" in msg or "not_found" in msg


def _account_was_swept(project: str, sa_email: str, exc: Exception) -> bool:
    """Return True only when full project inventory proves exact SA absence.

    Cloud IAM can emit ``NotFound`` while a freshly created identity or binding
    is still propagating. Treat that response as an absence candidate, then use
    the fully paginated project inventory as the deciding signal. Present or
    unreadable inventory keeps the accepted email owned and inside the shared
    propagation retry budget.
    """
    return _missing_account_signal(exc) and service_account_absent(project, sa_email) is True


def _establish_impersonation(
    source_credentials: Any,
    project: str,
    tenant: Tenant,
    member: str,
    *,
    propagation_deadline: float | None = None,
) -> Any:
    """Grant tokenCreator on one tenant SA and mint its token (sweep-resilient).

    The run principal is granted ``serviceAccountTokenCreator`` on the self-created
    SA, then an impersonated credential is refreshed (minting a short-lived token)
    within the shared IAM-propagation budget — the just-created self-grant is
    eventually consistent, so the first refresh can 403 for a few minutes. If a
    concurrent owner sweep deletes the SA inside that window (surfaced as a missing
    account), a fresh identity is minted and the grant+refresh retried, bounded to
    ``_MAX_SA_GENERATIONS`` generations. ``tenant.sa_email`` is left pointing at
    the surviving SA. The minted token is what the deny probes use; once minted it
    stays valid for the brief probe window even if the SA is later swept.
    """
    request = google.auth.transport.requests.Request()
    deadline = _shared_propagation_deadline(propagation_deadline)
    last_err: Exception | None = None
    for generation in range(1, _MAX_SA_GENERATIONS + 1):
        swept = False
        grant_ready = False
        for grant_attempt in range(1, IAM_PROPAGATION_ATTEMPTS + 1):
            try:

                def _record_token_creator_acceptance() -> None:
                    tenant.token_creator_grant_added = True
                    tenant.token_creator_member = member

                _grant_token_creator(
                    tenant.sa_email,
                    member,
                    on_accepted=_record_token_creator_acceptance,
                )
                grant_ready = True
                break
            except Exception as e:
                last_err = e
                if _account_was_swept(project, tenant.sa_email, e):
                    swept = True
                    break
                if not _missing_account_signal(e) or not _wait_for_propagation(
                    attempt=grant_attempt,
                    deadline=deadline,
                ):
                    break
        if grant_ready:
            credentials = _build_tenant_credentials(source_credentials, tenant.sa_email)
            for attempt in range(1, IAM_PROPAGATION_ATTEMPTS + 1):
                try:
                    credentials.refresh(request=request)
                    return credentials
                except Exception as e:  # propagation hedge + sweep detection
                    last_err = e
                    if _account_was_swept(project, tenant.sa_email, e):
                        swept = True
                        break
                    if _wait_for_propagation(attempt=attempt, deadline=deadline):
                        continue
                    break
        if not swept:
            break  # a non-sweep failure will not be fixed by a fresh identity
        if generation < _MAX_SA_GENERATIONS:
            _create_tenant_sa(project, tenant)
    raise RuntimeError(
        f"impersonation token for {tenant.sa_email} did not become usable within the shared "
        f"{_IAM_PROPAGATION_BUDGET_SECONDS}s IAM propagation budget: {last_err}"
    )


if __name__ == "__main__":
    sys.exit(main())
