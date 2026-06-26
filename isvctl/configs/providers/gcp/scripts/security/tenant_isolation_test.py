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
Compute Engine instance. The run credential impersonates service account A and
runs four cross-tenant probes against tenant B's resources:

  * network_isolated: each tenant's VPC carries no cross-tenant peering and no
    custom route toward the other tenant's primary range (config-plane
    inspection, mirrors the AWS orchestrator no-peering / no-shared-route check).
    Two custom-mode VPCs in the same project are mutually unreachable unless a
    peering or an explicit cross-tenant route is wired in.
  * data_isolated: SA-A is denied ``get_crypto_key`` on tenant B's CryptoKey
    (Cloud KMS raises PermissionDenied / 403).
  * compute_isolated: SA-A is denied ``instances.get`` and ``instances.stop`` on
    tenant B's instance.
  * storage_isolated: SA-A is denied ``get_bucket`` on tenant B's bucket. Cloud
    Storage hides a bucket's existence from a zero-grant principal as a 404, so a
    NotFound is accepted as an isolation pass alongside the 403.

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
from common.errors import delete_with_retry, handle_gcp_errors
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
from common.service_account import (
    IAM_PROPAGATION_ATTEMPTS,
    IAM_PROPAGATION_DELAY,
    create_service_account,
    delete_service_account,
    resolve_principal_member,
)
from google.api_core import exceptions as gax
from google.cloud import compute_v1, iam_admin_v1, kms_v1, storage
from google.iam.v1 import iam_policy_pb2, policy_pb2

# Role the run principal needs on tenant A's service account to impersonate it
# (mint short-lived tokens). Bound explicitly so the deny probes are
# self-contained rather than relying on an implicit project-level grant.
_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"

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
_CREATED_BY_LABEL = {"created-by": "isvtest"}

# Display name stamped on every tenant service account.
_SA_DISPLAY_NAME = "ISV SEC11-01 tenant isolation fixture"

# A concurrent owner sweep (a sibling security worker sharing this RUN_ID) can
# delete tenant A's service account inside the grant/propagation window before its
# impersonation token is minted. The token is minted from a freshly created SA, so
# if the SA is swept mid-window it is re-created with a new name and the mint is
# retried, bounded to this many identity generations.
_MAX_SA_GENERATIONS = 3


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


def _sa_account_id(base: str) -> str:
    """Return a <=30-char SA local part: base + run-id suffix + 4-hex discriminator."""
    # unique_suffix already truncates the run-id to 8 chars; the extra
    # token_hex(2) keeps two same-run-id invocations from colliding on the SA id.
    return f"{unique_suffix(base, length=6)}-{secrets.token_hex(2)}"


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


def _create_crypto_key(ring_path: str, key_id: str) -> str:
    """Create a symmetric ENCRYPT_DECRYPT CryptoKey and return its resource path.

    Cloud KMS keys cannot be hard-deleted, so on a same-run re-run an
    AlreadyExists falls back to a get so the existing key is reused.
    """
    client = kms_v1.KeyManagementServiceClient()
    crypto_key = kms_v1.CryptoKey(
        purpose=kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT,
        version_template=kms_v1.CryptoKeyVersionTemplate(
            algorithm=kms_v1.CryptoKeyVersion.CryptoKeyVersionAlgorithm.GOOGLE_SYMMETRIC_ENCRYPTION,
        ),
    )
    try:
        created = client.create_crypto_key(parent=ring_path, crypto_key_id=key_id, crypto_key=crypto_key)
        return created.name
    except gax.AlreadyExists:
        return client.get_crypto_key(name=f"{ring_path}/cryptoKeys/{key_id}").name


def _create_bucket(client: storage.Client, project: str, location: str, bucket_name: str) -> None:
    """Create a labelled, uniform-access Cloud Storage bucket for a tenant."""
    bucket = client.bucket(bucket_name)
    bucket.labels = dict(_CREATED_BY_LABEL)
    bucket.iam_configuration.uniform_bucket_level_access_enabled = True
    client.create_bucket(bucket, project=project, location=location)


def _create_tenant_sa(project: str, tenant: Tenant) -> None:
    """Create the tenant's cross-tenant probe identity (its service account).

    Created last in the provisioning order (after the heavy VPC/instance fixtures)
    and immediately before impersonation so the window in which a concurrent owner
    sweep can delete an in-use SA is as small as possible.
    """
    account_id = _sa_account_id(tenant.sa_base)
    tenant.sa_email = create_service_account(project, account_id, display_name=_SA_DISPLAY_NAME)
    tenant.created["sa"] = True


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
    step. Each created flag is stamped on ``tenant.created`` immediately after (or,
    for the async insert+wait resources, before) each create so a crash
    mid-provision still hands teardown the ownership signal.
    """
    # VPC + subnet: insert_network creates a custom-mode network (no auto
    # subnets) and stamps the description ownership marker (the Network proto
    # carries no labels field). A custom-mode NIC needs an explicit regional
    # subnetwork, so each tenant gets its own subnet on a distinct primary range.
    region = zone_to_region(tenant.zone)
    # Stamp the VPC/subnet/instance trackers BEFORE each async insert: those
    # route through _wait_or_rollback, which can raise (wait-fail + rollback-fail)
    # before a stamp-after would run, orphaning the resource from both this
    # finally block and the teardown safety-net. The synchronous KMS-key / bucket
    # creates below return only on success, so they stamp after.
    tenant.vpc_name = unique_suffix(tenant.vpc_base)
    tenant.created["vpc"] = True
    insert_network(project, tenant.vpc_name)

    tenant.subnet_name = unique_suffix(tenant.subnet_base)
    tenant.created["subnet"] = True
    insert_subnetwork(project, region, tenant.subnet_name, tenant.vpc_name, tenant.subnet_cidr)

    # Cloud KMS CryptoKey in the shared per-run ring.
    key_id = unique_suffix(tenant.key_base)
    tenant.key_name = _create_crypto_key(ring_path, key_id)
    tenant.created["kms_key"] = True

    # Cloud Storage bucket.
    tenant.bucket_name = unique_suffix(tenant.bucket_base)
    _create_bucket(storage_client, project, location, tenant.bucket_name)
    tenant.created["bucket"] = True

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
    # Stamp before the async insert/wait (see the VPC/subnet note above).
    tenant.created["instance"] = True
    insert_instance(project, tenant.zone, instance)


def _delete_compute_idempotent(
    delete_fn: Callable[..., Any],
    get_fn: Callable[..., Any],
    *args: Any,
    resource_desc: str,
) -> str | None:
    """Delete an owned Compute resource, folding an already-gone resource into success.

    ``delete_fn`` (``delete_instance`` / ``delete_subnetwork`` / ``delete_network``)
    catches a synchronous ``NotFound``, but a concurrent owner sweep (a sibling
    security worker sharing this RUN_ID) can delete the resource so its delete-op
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

    if tenant.created.get("sa") and tenant.sa_email:
        # delete_service_account folds an already-gone SA (a concurrent owner sweep
        # removed it — existence-hiding 403, never NotFound) into success, so a
        # False return is a genuine, persistent teardown failure.
        if not delete_service_account(tenant.sa_email):
            errors.append(f"delete service account {tenant.sa_email}")

    return errors


def _grant_token_creator(sa_email: str, member: str) -> None:
    """Grant ``member`` ``serviceAccountTokenCreator`` on ``sa_email`` so it can be impersonated."""
    iam = iam_admin_v1.IAMClient()
    resource = f"projects/-/serviceAccounts/{sa_email}"
    policy = iam.get_iam_policy(request=iam_policy_pb2.GetIamPolicyRequest(resource=resource))
    for binding in policy.bindings:
        if binding.role == _TOKEN_CREATOR_ROLE and member in binding.members:
            return  # already granted
    policy.bindings.append(policy_pb2.Binding(role=_TOKEN_CREATOR_ROLE, members=[member]))
    iam.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy))


def _build_tenant_a_credentials(source_credentials: Any, sa_email: str) -> Any:
    """Return impersonated credentials for tenant A's service account.

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


def _probe_data_isolation(credentials_a: Any, tenant_b: Tenant) -> dict[str, Any]:
    """Tenant A must be denied ``get_crypto_key`` on tenant B's CryptoKey."""
    client = kms_v1.KeyManagementServiceClient(credentials=credentials_a)
    probe: dict[str, Any] = {"name": "kms_get_crypto_key_denied"}
    try:
        client.get_crypto_key(name=tenant_b.key_name)
    except Exception as exc:  # any access error is the deny signal under test
        probe["passed"] = _is_denied(exc)
        probe["code"] = type(exc).__name__
        if not probe["passed"]:
            probe["error"] = str(exc)
    else:
        probe["passed"] = False
        probe["error"] = "get_crypto_key on tenant B key unexpectedly succeeded"
    return {"passed": probe["passed"], "probes": [probe]}


def _probe_compute_isolation(credentials_a: Any, project: str, tenant_b: Tenant) -> dict[str, Any]:
    """Tenant A must be denied ``instances.get`` and ``instances.stop`` on tenant B's instance."""
    client = compute_v1.InstancesClient(credentials=credentials_a)
    probes: list[dict[str, Any]] = []

    get_probe: dict[str, Any] = {"name": "instances_get_denied"}
    try:
        client.get(project=project, zone=tenant_b.zone, instance=tenant_b.instance_name)
    except Exception as exc:  # any access error is the deny signal under test
        get_probe["passed"] = _is_denied(exc)
        get_probe["code"] = type(exc).__name__
        if not get_probe["passed"]:
            get_probe["error"] = str(exc)
    else:
        get_probe["passed"] = False
        get_probe["error"] = "instances.get on tenant B instance unexpectedly succeeded"
    probes.append(get_probe)

    stop_probe: dict[str, Any] = {"name": "instances_stop_denied"}
    try:
        client.stop(project=project, zone=tenant_b.zone, instance=tenant_b.instance_name)
    except Exception as exc:  # any access error is the deny signal under test
        stop_probe["passed"] = _is_denied(exc)
        stop_probe["code"] = type(exc).__name__
        if not stop_probe["passed"]:
            stop_probe["error"] = str(exc)
    else:
        stop_probe["passed"] = False
        stop_probe["error"] = "instances.stop on tenant B instance unexpectedly succeeded"
    probes.append(stop_probe)

    return {"passed": all(p["passed"] for p in probes), "probes": probes}


def _probe_storage_isolation(credentials_a: Any, project: str, tenant_b: Tenant) -> dict[str, Any]:
    """Tenant A must be denied ``get_bucket`` on tenant B's bucket.

    Cloud Storage hides a bucket's existence from a zero-grant principal as a
    404, so a NotFound is accepted as an isolation pass alongside the 403.
    """
    client = storage.Client(project=project, credentials=credentials_a)
    probe: dict[str, Any] = {"name": "storage_get_bucket_denied"}
    try:
        client.get_bucket(tenant_b.bucket_name)
    except gax.NotFound:
        # Bucket existence hidden from a zero-grant principal — isolation holds.
        probe["passed"] = True
        probe["code"] = "NotFound"
    except Exception as exc:  # any access error is the deny signal under test
        probe["passed"] = _is_denied(exc) or _looks_like_404(exc)
        probe["code"] = type(exc).__name__
        if not probe["passed"]:
            probe["error"] = str(exc)
    else:
        probe["passed"] = False
        probe["error"] = "get_bucket on tenant B bucket unexpectedly succeeded"
    return {"passed": probe["passed"], "probes": [probe]}


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
            ring_path = _key_ring_path(project, location, unique_suffix(_KEY_RING_BASE, length=6))
            # Provision the heavy fixtures (VPC/subnet/KMS/bucket/instance) FIRST so
            # each tenant's probe targets exist, then create the service accounts
            # LAST — just before they are impersonated — to minimize the window in
            # which a concurrent owner sweep (a sibling security worker sharing this
            # RUN_ID) can delete an in-use SA.
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
            # Grant token-creator on SA-A and mint its impersonation token,
            # re-creating SA-A if a concurrent owner sweep deletes it inside the
            # propagation window. tenant_a.sa_email points at the surviving SA
            # afterward, so the contract ids are recorded after this call.
            credentials_a = _establish_impersonation(source_credentials, project, tenant_a, resolve_principal_member())
            result["tenant_a_id"] = tenant_a.sa_email
            result["tenant_b_id"] = tenant_b.sa_email

            result["tests"]["network_isolated"] = _probe_network_isolation(project, tenant_a, tenant_b)
            result["tests"]["data_isolated"] = _probe_data_isolation(credentials_a, tenant_b)
            result["tests"]["compute_isolated"] = _probe_compute_isolation(credentials_a, project, tenant_b)
            result["tests"]["storage_isolated"] = _probe_storage_isolation(credentials_a, project, tenant_b)
    except Exception as e:
        result["error"] = str(e)
    finally:
        cleanup_errors: list[str] = []
        for tenant in (tenant_a, tenant_b):
            cleanup_errors.extend(_teardown_tenant(project=project, tenant=tenant))
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors

    # A structured skip wins only when nothing leaked, so the validator skips
    # rather than evaluating an empty result.
    if skip_payload is not None and not result.get("cleanup_errors"):
        print(json.dumps(skip_payload, indent=2))
        return 0

    # Recompute success after the try/except/finally completes: grounded in the
    # four subtests AND failed when cleanup leaked a fixture (unless the run was
    # a clean skip, handled above). Mirrors the AWS oracle + teardown safety-net.
    result["success"] = all(t.get("passed") for t in result["tests"].values()) and not result.get("cleanup_errors")
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


def _account_was_swept(exc: Exception) -> bool:
    """Return True iff ``exc`` shows tenant A's service account no longer exists.

    A concurrent owner sweep (a sibling security worker sharing this RUN_ID) can
    delete the SA before its token is minted. Cloud IAM reports a deleted SA as
    ``404 NOT_FOUND`` / "Account deleted: <id>" from ``generateAccessToken`` and as
    ``NotFound`` from the IAM-policy surface. A grant-not-yet-propagated error is a
    ``403`` (absorbed by the propagation retry, not here), so it is deliberately
    NOT matched — only a missing-account signal triggers a re-create.
    """
    if isinstance(exc, gax.NotFound):
        return True
    msg = str(exc).lower()
    return "account deleted" in msg or "not_found" in msg


def _establish_impersonation(
    source_credentials: Any,
    project: str,
    tenant_a: Tenant,
    member: str,
) -> Any:
    """Grant tokenCreator on SA-A and mint its impersonation token (sweep-resilient).

    The run principal is granted ``serviceAccountTokenCreator`` on the self-created
    SA, then an impersonated credential is refreshed (minting a short-lived token)
    within the shared IAM-propagation budget — the just-created self-grant is
    eventually consistent, so the first refresh can 403 for a few minutes. If a
    concurrent owner sweep deletes SA-A inside that window (surfaced as a missing
    account), a fresh identity is minted and the grant+refresh retried, bounded to
    ``_MAX_SA_GENERATIONS`` generations. ``tenant_a.sa_email`` is left pointing at
    the surviving SA. The minted token is what the deny probes use; once minted it
    stays valid for the brief probe window even if the SA is later swept.
    """
    request = google.auth.transport.requests.Request()
    last_err: Exception | None = None
    for generation in range(1, _MAX_SA_GENERATIONS + 1):
        swept = False
        try:
            _grant_token_creator(tenant_a.sa_email, member)
            credentials_a = _build_tenant_a_credentials(source_credentials, tenant_a.sa_email)
            for attempt in range(1, IAM_PROPAGATION_ATTEMPTS + 1):
                try:
                    credentials_a.refresh(request)
                    return credentials_a
                except Exception as e:  # propagation hedge + sweep detection
                    last_err = e
                    if _account_was_swept(e):
                        swept = True
                        break
                    if attempt < IAM_PROPAGATION_ATTEMPTS:
                        time.sleep(IAM_PROPAGATION_DELAY)
        except Exception as e:  # grant on an already-swept SA surfaces here
            last_err = e
            swept = _account_was_swept(e)
        if not swept:
            break  # a non-sweep failure will not be fixed by a fresh identity
        if generation < _MAX_SA_GENERATIONS:
            _create_tenant_sa(project, tenant_a)
    raise RuntimeError(
        f"impersonation token for tenant A did not become usable within "
        f"{IAM_PROPAGATION_ATTEMPTS * IAM_PROPAGATION_DELAY}s: {last_err}"
    )


if __name__ == "__main__":
    sys.exit(main())
