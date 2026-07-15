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

"""Security test teardown (safety-net sweep).

Each security test step already cleans up its own fixtures in a ``finally``
block, so this teardown is only a safety net for resources that a hard crash
left behind. It scans the security fixture families and deletes a resource ONLY
when dual-gate ownership holds:

  * the resource name carries the canonical alphanumeric ``RUN_ID[:8]`` as its
    exact terminal ``-<token>`` or ``_<token>`` suffix (service-account entropy
    appears before that suffix), AND
  * the resource carries its native ``created-by=isvtest`` provenance: labels
    on CryptoKeys, Compute disks/instances, and GCS buckets; exact descriptions
    on networks/subnetworks, service accounts, workload-identity pools, and
    custom roles; or the exact marked role/member/condition/bucket tuple on the
    project IAM binding. Workload-identity providers use an exact fixture
    description under an already-owned pool.

A resource that matches a fixture name prefix but is MISSING the created-by
marker / run-id token belongs to another run (or to the operator) and is
counted into ``resources_skipped_unowned`` and NEVER deleted -- an honesty
signal that the sweep saw it but declined ownership.

Every fixture family is swept unconditionally; the per-fixture created flags the
provider config forwards are advisory only (a standalone ``--phase teardown``
after a crash runs in a process where the test steps never set them). The
dual-gate ownership check above is the guard against touching resources this
run did not create. Because that check owns a resource only when its name
embeds the run-id token, a standalone sweep REQUIRES the original run's
``RUN_ID``/``LS_RUN_ID`` to be re-exported; with no run id the sweep fails closed
(it would otherwise be a success-looking no-op that leaves preserved fixtures
behind) rather than reporting a hollow success.

Families swept:

  * Cloud KMS: CryptoKeys named ``isv-sec09-*`` / ``isv-sec11-*``. KMS keys and
    key rings cannot be hard-deleted, so the SEC09 Compute service-agent grant
    is removed and the key's versions are scheduled for destruction.
  * Compute disks ``isv-sec09-disk-*-<run>``, instances
    ``isv-sec11-*-vm-<run>``, and networks ``isv-sec11-*-vpc-<run>`` (every name
    is run-id suffixed via ``unique_suffix``, so the fixture word is an INFIX,
    not a suffix). An instance is deleted before its VPC, and a VPC's dependent
    subnetworks are deleted before the VPC itself.
  * SEC02 workload-identity providers/pools ``isv-sec02-wif-*`` (providers are
    deleted before their parent pool), service accounts ``isv-sec02-node-*`` /
    ``isv-sec04-*`` / ``isv-sec11-*``, and custom roles ``isv_sec04_*``.
  * GCS buckets ``isv-sec04-*`` / ``isv-sec11-*`` (objects emptied before the
    bucket is removed).

Every delete flows through ``common.errors.delete_with_retry`` (a NotFound is
the desired terminal state and counts as success; transient errors are
retried). Compute deletes (disk/instance/subnetwork/network) go through the
waited ``common.network`` helpers, which block on the returned async op until it
reaches DONE, so a ``resources_cleaned`` increment means the resource is
observably gone -- not merely that the delete call was accepted. The sweep is
best-effort: one failing delete does not abort the others.

Usage:
    python3 teardown.py --region us-central1 --project my-project
    python3 teardown.py --region us-central1 --skip-destroy

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "teardown",
    "resources_cleaned": 2,
    "resources_skipped_unowned": 0
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from typing import Any, cast

import _script_imports
import google.auth
from common.cmek_policy import compute_service_agent_member, remove_kms_role_member
from common.compute import resolve_project
from common.errors import delete_with_retry, handle_gcp_errors, modify_iam_policy_with_retry
from common.iam_policy import remove_binding_members, service_account_member_email
from common.kms import iter_kms_locations
from common.network import (
    delete_disk,
    delete_instance,
    delete_network,
    delete_subnetwork,
    list_subnetworks_for_network,
    network_has_isv_ownership,
    subnetwork_has_isv_ownership,
)
from common.ownership import (
    CREATED_BY_DESCRIPTION,
    CREATED_BY_LABEL,
    CREATED_BY_VALUE,
    INVOCATION_DESCRIPTION_KEY,
)
from common.service_account import delete_service_account
from google.api_core import exceptions as gax
from google.auth.transport.requests import AuthorizedSession
from google.cloud import compute_v1, iam_admin_v1, kms_v1, resourcemanager_v3, storage
from google.iam.v1 import iam_policy_pb2, options_pb2
from short_lived_credentials_support import (
    AuthorizedHttp,
    WorkloadIdentityRestClient,
    has_wif_pool_ownership,
)

SCRIPTS_DIR = _script_imports.SCRIPTS_DIR

# Fixture name prefixes the security test steps stamp on the resources they
# create (mirrors the AWS reference's owned-prefix sets).
KMS_KEY_PREFIXES: tuple[str, ...] = ("isv-sec09-cmk", "isv-sec11-")
DISK_PREFIX = "isv-sec09-disk"
INSTANCE_PREFIX = "isv-sec11-"
NETWORK_PREFIX = "isv-sec11-"
SEC02_NODE_SA_PREFIX = "isv-sec02-node-"
SEC02_WIF_POOL_PREFIX = "isv-sec02-wif-"
SEC02_WIF_PROVIDER_ID = "oidc"
SA_PREFIXES: tuple[str, ...] = (SEC02_NODE_SA_PREFIX, "isv-sec04-", "isv-sec11-")
ROLE_PREFIX = "isv_sec04_"
BUCKET_PREFIXES: tuple[str, ...] = ("isv-sec04-", "isv-sec11-")

# Resource-native provenance constants used by the family-specific second gate.
LP_BINDING_CONDITION_TITLE = "isv-sec04-least-privilege"
LP_BINDING_CONDITION_DESCRIPTION = f"SEC04 least-privilege scoped grant ({CREATED_BY_DESCRIPTION})."
LP_ROLE_DESCRIPTION = f"Scoped role for SEC04 least-privilege validation ({CREATED_BY_DESCRIPTION})."
SEC02_NODE_SA_DESCRIPTION = f"SEC02 node-equivalent credential fixture ({CREATED_BY_DESCRIPTION})."
TENANT_KMS_ROLE = "roles/cloudkms.viewer"
_WORKLOAD_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


def _run_token() -> str:
    """Return the canonical 8-character run suffix used by fixture names.

    Run-scoped cleanup canonicalizes UUID run IDs to their leading eight
    hexadecimal characters, matching ``unique_suffix`` and custom-role IDs.
    Returns an empty string when no run id is set; ``main`` treats that as a
    fail-closed condition rather than running an ownership-free sweep.
    """
    sid = os.environ.get("RUN_ID") or os.environ.get("LS_RUN_ID") or ""
    cleaned = "".join(c for c in sid.lower() if c.isalnum())
    return cleaned[:8]


def _name_owned_by_run(name: str | None, run_token: str, prefixes: tuple[str, ...]) -> bool:
    """Return True iff ``name`` has an owned prefix and terminal run suffix."""
    if not name or not run_token:
        return False
    if not name.startswith(prefixes):
        return False
    lowered = name.lower()
    return lowered.endswith(f"-{run_token}") or lowered.endswith(f"_{run_token}")


def _has_created_by_label(labels: Any) -> bool:
    """Return True iff ``labels`` carries the ``created-by=isvtest`` ownership marker."""
    return dict(labels or {}).get(CREATED_BY_LABEL) == CREATED_BY_VALUE


def _has_owned_description(description: Any, base: str) -> bool:
    """Match a legacy ownership description or its exact invocation-marked form."""
    value = str(description or "")
    if value == base:
        return True
    pattern = rf"{re.escape(base)} \({re.escape(INVOCATION_DESCRIPTION_KEY)}=[0-9a-f]{{32}}\)"
    return re.fullmatch(pattern, value) is not None


def _remove_tenant_kms_members(
    client: kms_v1.KeyManagementServiceClient,
    key_name: str,
    project: str,
    run_suffix: str,
) -> bool:
    """Remove only run-owned tenant-SA viewers from one owned SEC11 key."""
    targeted = False

    def _read() -> Any:
        return client.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(
                resource=key_name,
                options=options_pb2.GetPolicyOptions(requested_policy_version=3),
            )
        )

    def _write(policy: Any) -> Any:
        policy.version = 3
        return client.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=key_name, policy=policy))

    def _owned_member(member: str) -> bool:
        email = service_account_member_email(member)
        if not email.endswith(f"@{project}.iam.gserviceaccount.com"):
            return False
        return _name_owned_by_run(email.split("@", 1)[0], run_suffix, ("isv-sec11-",))

    def _remove(policy: Any) -> bool:
        nonlocal targeted
        removed = remove_binding_members(
            policy,
            binding_matches=lambda binding: (
                binding.role == TENANT_KMS_ROLE
                and not (
                    getattr(getattr(binding, "condition", None), "title", "")
                    or getattr(getattr(binding, "condition", None), "description", "")
                    or getattr(getattr(binding, "condition", None), "expression", "")
                )
            ),
            member_matches=_owned_member,
        )
        targeted = targeted or bool(removed)
        return bool(removed)

    modify_iam_policy_with_retry(_read, _write, _remove, resource_desc=f"CryptoKey {key_name}")
    return targeted


def _sweep_kms_keys(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Remove SEC09 grants and destroy versions for owned, undeletable CryptoKeys.

    KMS has no flat key list: walk locations -> key rings -> crypto keys. A key
    is owned only when its short name matches the run scope and its labels carry
    the created-by provenance marker.
    """
    errors: list[str] = []
    client = kms_v1.KeyManagementServiceClient()
    service_agent_member: str | None = None
    for location in iter_kms_locations(client, project):
        try:
            for key_ring in client.list_key_rings(parent=location.name):
                for crypto_key in client.list_crypto_keys(parent=key_ring.name):
                    short = crypto_key.name.rsplit("/", 1)[-1]
                    if not short.startswith(KMS_KEY_PREFIXES):
                        continue
                    owned = _name_owned_by_run(short, run_suffix, KMS_KEY_PREFIXES) and _has_created_by_label(
                        getattr(crypto_key, "labels", None)
                    )
                    if not owned:
                        counters["skipped"] += 1
                        continue
                    key_clean = True
                    if short.startswith("isv-sec09-cmk"):
                        try:
                            if service_agent_member is None:
                                service_agent_member = compute_service_agent_member(project)
                            remove_kms_role_member(client, crypto_key.name, service_agent_member)
                        except Exception as exc:
                            errors.append(
                                f"remove Compute service-agent grant from {crypto_key.name}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                            key_clean = False
                    elif short.startswith("isv-sec11-"):
                        try:
                            _remove_tenant_kms_members(client, crypto_key.name, project, run_suffix)
                        except Exception as exc:
                            errors.append(
                                f"remove tenant viewer grants from {crypto_key.name}: {type(exc).__name__}: {exc}"
                            )
                            key_clean = False
                    if not _destroy_key_versions(client, crypto_key.name, errors):
                        key_clean = False
                    if key_clean:
                        counters["cleaned"] += 1
        except Exception as exc:
            # Keep sweeping later locations and independent resource families,
            # but never translate an incomplete KMS inventory into successful
            # cleanup. Lazy pager failures also surface inside this try block.
            errors.append(f"enumerate KMS location {location.name}: {type(exc).__name__}: {exc}")
    return errors


def _destroy_key_versions(
    client: kms_v1.KeyManagementServiceClient,
    key_name: str,
    errors: list[str],
) -> bool:
    """Schedule every ENABLED version of an owned key for destruction (best-effort).

    Returns True iff the key had its versions scheduled (or there were none left
    to schedule) without an unrecoverable error.
    """
    enabled_state = kms_v1.CryptoKeyVersion.CryptoKeyVersionState.ENABLED
    ok = True
    for version in client.list_crypto_key_versions(parent=key_name):
        if version.state != enabled_state:
            continue
        if not delete_with_retry(
            client.destroy_crypto_key_version,
            name=version.name,
            resource_desc=f"key version {version.name}",
        ):
            errors.append(f"destroy key version {version.name} failed")
            ok = False
    return ok


def _sweep_disks(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned Compute disks (dual-gate: run-id-suffixed name AND created-by label)."""
    errors: list[str] = []
    client = compute_v1.DisksClient()
    for zone_scope, scoped in client.aggregated_list(project=project):
        zone = zone_scope.rsplit("/", 1)[-1]
        for disk in getattr(scoped, "disks", None) or []:
            if not disk.name.startswith(DISK_PREFIX):
                continue
            owned = _name_owned_by_run(disk.name, run_suffix, (DISK_PREFIX,)) and _has_created_by_label(
                getattr(disk, "labels", None)
            )
            if not owned:
                counters["skipped"] += 1
                continue
            # delete_disk waits for the async zonal delete op to reach DONE, so a
            # cleaned count is only incremented after the disk is observably gone.
            if delete_with_retry(
                delete_disk,
                project,
                zone,
                disk.name,
                resource_desc=f"disk {disk.name}",
            ):
                counters["cleaned"] += 1
            else:
                errors.append(f"delete disk {disk.name} failed")
    return errors


def _sweep_instances(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned Compute instances. Must run BEFORE the VPC sweep (a VM pins its network)."""
    errors: list[str] = []
    client = compute_v1.InstancesClient()
    for zone_scope, scoped in client.aggregated_list(project=project):
        zone = zone_scope.rsplit("/", 1)[-1]
        for instance in getattr(scoped, "instances", None) or []:
            # Tenant probe VMs end with the runtime RUN_ID suffix via
            # unique_suffix, so the run id trails the ``-vm`` segment — match the
            # ``-vm-`` infix, not a ``-vm`` suffix (which never holds post-suffix).
            if not instance.name.startswith(INSTANCE_PREFIX) or "-vm-" not in instance.name:
                continue
            owned = _name_owned_by_run(instance.name, run_suffix, (INSTANCE_PREFIX,)) and _has_created_by_label(
                getattr(instance, "labels", None)
            )
            if not owned:
                counters["skipped"] += 1
                continue
            # delete_instance waits for the async zonal delete op to reach DONE,
            # so the cleaned count and the subsequent VPC sweep only proceed once
            # the VM (which pins its network) is observably gone.
            if delete_with_retry(
                delete_instance,
                project,
                zone,
                instance.name,
                resource_desc=f"instance {instance.name}",
            ):
                counters["cleaned"] += 1
            else:
                errors.append(f"delete instance {instance.name} failed")
    return errors


def _sweep_networks(project: str, region: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete owned VPC networks (and their dependent subnetworks first).

    Networks and subnetworks carry description provenance rather than labels;
    each resource independently requires name/run scope plus that marker. Run
    AFTER the instance sweep so a dependent VM has been removed first.

    Tenant VPCs are created as ``unique_suffix("isv-sec11-<tenant>-vpc")``, so
    the RUN_ID suffix trails the ``-vpc`` segment. Match
    the ``-vpc-`` infix, not a ``-vpc`` suffix (which never holds post-suffix and
    would skip every owned VPC before the ownership gate even runs).

    A custom-mode VPC cannot be deleted while it still owns subnetworks, so the
    owned network's subnetworks in ``region`` are deleted first. Both deletes go
    through waited helpers (``delete_subnetwork`` / ``delete_network`` block on
    the async op until DONE) so a cleaned count is only incremented after the
    resource is observably gone.
    """
    errors: list[str] = []
    client = compute_v1.NetworksClient()
    for network in client.list(project=project):
        if not network.name.startswith(NETWORK_PREFIX) or "-vpc-" not in network.name:
            continue
        owned_network = _name_owned_by_run(network.name, run_suffix, (NETWORK_PREFIX,)) and network_has_isv_ownership(
            network
        )
        if not owned_network:
            counters["skipped"] += 1
            continue
        # Delete dependent subnetworks before the VPC (a custom-mode network pins
        # them). Subnetworks are regional; the run's --region is where the tenant
        # subnets were created. Skip the subnet pass only when no region is known.
        all_subnets_owned = True
        if region:
            for subnet in list_subnetworks_for_network(project, region, network.name):
                owned_subnet = (
                    _name_owned_by_run(subnet.name, run_suffix, (NETWORK_PREFIX,))
                    and "-subnet-" in subnet.name
                    and subnetwork_has_isv_ownership(subnet)
                )
                if not owned_subnet:
                    counters["skipped"] += 1
                    all_subnets_owned = False
                    errors.append(f"preserve unowned subnetwork {subnet.name} attached to owned network {network.name}")
                    continue
                if delete_with_retry(
                    delete_subnetwork,
                    project,
                    region,
                    subnet.name,
                    resource_desc=f"subnetwork {subnet.name}",
                ):
                    counters["cleaned"] += 1
                else:
                    errors.append(f"delete subnetwork {subnet.name} failed")
                    all_subnets_owned = False
        if not all_subnets_owned:
            errors.append(f"preserve network {network.name} because a dependent subnetwork was not removed")
            continue
        if delete_with_retry(
            delete_network,
            project,
            network.name,
            resource_desc=f"network {network.name}",
        ):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete network {network.name} failed")
    return errors


def _sweep_service_accounts(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete service accounts only after name/run and description provenance match."""
    errors: list[str] = []
    iam = iam_admin_v1.IAMClient()
    for sa in iam.list_service_accounts(name=f"projects/{project}"):
        local_part = sa.email.split("@", 1)[0]
        if not local_part.startswith(SA_PREFIXES):
            continue
        description_base = (
            SEC02_NODE_SA_DESCRIPTION if local_part.startswith(SEC02_NODE_SA_PREFIX) else CREATED_BY_DESCRIPTION
        )
        owned = _name_owned_by_run(local_part, run_suffix, SA_PREFIXES) and _has_owned_description(
            getattr(sa, "description", ""), description_base
        )
        if not owned:
            counters["skipped"] += 1
            continue
        if delete_service_account(sa.email, project=project):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete service account {sa.email} failed")
    return errors


def _project_number(project: str) -> str:
    """Resolve a project ID to the numeric parent required by IAM WIF APIs."""
    resource = resourcemanager_v3.ProjectsClient().get_project(name=f"projects/{project}")
    number = str(resource.name).rsplit("/", 1)[-1]
    if not number.isdigit():
        raise RuntimeError(f"project lookup returned an invalid project number: {resource.name!r}")
    return number


def _workload_identity_fixture_client(project: str) -> WorkloadIdentityRestClient:
    """Build the authorized SEC02 workload-identity inventory adapter."""
    credentials, _ = google.auth.default(scopes=list(_WORKLOAD_SCOPES))
    session = cast(AuthorizedHttp, AuthorizedSession(credentials))
    return WorkloadIdentityRestClient(session, _project_number(project))


def _sweep_workload_identity_pools(
    project: str,
    run_suffix: str,
    counters: dict[str, int],
) -> list[str]:
    """Delete owned SEC02 providers before their owned parent pools."""
    errors: list[str] = []
    client = _workload_identity_fixture_client(project)
    for pool in client.list_pools():
        pool_id = str(pool.get("name") or "").rsplit("/", 1)[-1]
        if not pool_id.startswith(SEC02_WIF_POOL_PREFIX):
            continue
        owned_pool = _name_owned_by_run(
            pool_id,
            run_suffix,
            (SEC02_WIF_POOL_PREFIX,),
        ) and has_wif_pool_ownership(pool.get("description"))
        if not owned_pool:
            counters["skipped"] += 1
            continue

        try:
            providers = client.list_providers(pool_id)
        except Exception as exc:
            errors.append(f"provider inventory for workload identity pool {pool_id} failed: {exc}")
            continue

        providers_owned = True
        owned_provider_ids: list[str] = []
        pool_description = str(pool.get("description") or "")
        for provider in providers:
            provider_id = str(provider.get("name") or "").rsplit("/", 1)[-1]
            owned_provider = provider_id == SEC02_WIF_PROVIDER_ID and provider.get("description") == pool_description
            if not owned_provider:
                counters["skipped"] += 1
                providers_owned = False
                errors.append(
                    f"preserve workload identity pool {pool_id} because provider "
                    f"{provider_id or '<unnamed>'} is unowned"
                )
                continue
            owned_provider_ids.append(provider_id)

        if not providers_owned:
            continue

        providers_removed = True
        for provider_id in owned_provider_ids:
            try:
                client.delete_provider(pool_id, provider_id)
            except Exception as exc:
                providers_removed = False
                errors.append(f"delete workload identity provider {pool_id}/{provider_id} failed: {exc}")
            else:
                counters["cleaned"] += 1
        if not providers_removed:
            errors.append(f"preserve workload identity pool {pool_id} because a provider was not removed")
            continue

        try:
            client.delete_pool(pool_id)
        except Exception as exc:
            errors.append(f"delete workload identity pool {pool_id} failed: {exc}")
        else:
            counters["cleaned"] += 1
    return errors


def _sweep_custom_roles(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Delete custom roles only after name/run and description provenance match."""
    errors: list[str] = []
    iam = iam_admin_v1.IAMClient()
    for role in iam.list_roles(request={"parent": f"projects/{project}"}):
        role_id = role.name.rsplit("/", 1)[-1]  # projects/<p>/roles/<id> -> <id>
        if not role_id.startswith(ROLE_PREFIX):
            continue
        owned = _name_owned_by_run(role_id, run_suffix, (ROLE_PREFIX,)) and (
            _has_owned_description(getattr(role, "description", ""), LP_ROLE_DESCRIPTION)
        )
        if not owned:
            counters["skipped"] += 1
            continue
        if delete_with_retry(
            iam.delete_role,
            name=role.name,
            resource_desc=f"custom role {role.name}",
        ):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete custom role {role.name} failed")
    return errors


class _NoOwnedBinding(Exception):
    """Internal signal that a policy read contains no binding owned by this run."""


def _binding_bucket_name(expression: str) -> str:
    """Extract the SEC04 bucket name from the fixture's CEL expression."""
    match = re.search(r'buckets/([^"\s)]+)', expression)
    return match.group(1) if match else ""


def _sweep_project_iam_bindings(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Remove only run-owned SEC04 members from the project IAM policy."""
    resource = f"projects/{project}"
    projects = resourcemanager_v3.ProjectsClient()
    removed_count = 0
    targeted_once = False

    def _read() -> Any:
        return projects.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(
                resource=resource,
                options=options_pb2.GetPolicyOptions(requested_policy_version=3),
            )
        )

    def _write(policy: Any) -> Any:
        policy.version = 3
        return projects.set_iam_policy(request={"resource": resource, "policy": policy})

    def _owned_binding(binding: Any) -> bool:
        role_id = str(getattr(binding, "role", "")).rsplit("/", 1)[-1]
        condition = getattr(binding, "condition", None)
        expression = str(getattr(condition, "expression", "") or "")
        return (
            _name_owned_by_run(role_id, run_suffix, (ROLE_PREFIX,))
            and getattr(condition, "title", "") == LP_BINDING_CONDITION_TITLE
            and str(getattr(condition, "description", "") or "") == LP_BINDING_CONDITION_DESCRIPTION
            and _name_owned_by_run(_binding_bucket_name(expression), run_suffix, ("isv-sec04-",))
        )

    def _owned_member(member: str) -> bool:
        email = service_account_member_email(member)
        if not email.endswith(f"@{project}.iam.gserviceaccount.com"):
            return False
        local_part = email.split("@", 1)[0]
        return _name_owned_by_run(local_part, run_suffix, ("isv-sec04-",))

    def _remove(policy: Any) -> None:
        nonlocal removed_count, targeted_once
        removed_count = remove_binding_members(
            policy,
            binding_matches=_owned_binding,
            member_matches=_owned_member,
        )
        if not removed_count:
            raise _NoOwnedBinding
        targeted_once = True

    try:
        modify_iam_policy_with_retry(
            _read,
            _write,
            _remove,
            resource_desc=f"project {project}",
        )
    except _NoOwnedBinding:
        if targeted_once:
            counters["cleaned"] += removed_count or 1
        return []
    except Exception as exc:
        return [f"remove project IAM binding: {type(exc).__name__}: {exc}"]

    counters["cleaned"] += removed_count
    return []


def _sweep_buckets(project: str, run_suffix: str, counters: dict[str, int]) -> list[str]:
    """Empty and delete owned GCS buckets (dual-gate: run-id-suffixed name AND created-by label)."""
    errors: list[str] = []
    client = storage.Client(project=project)
    for bucket in client.list_buckets():
        if not bucket.name.startswith(BUCKET_PREFIXES):
            continue
        owned = _name_owned_by_run(bucket.name, run_suffix, BUCKET_PREFIXES) and _has_created_by_label(
            getattr(bucket, "labels", None)
        )
        if not owned:
            counters["skipped"] += 1
            continue
        try:
            blobs = list(client.list_blobs(bucket.name))
        except gax.NotFound:
            counters["cleaned"] += 1
            continue
        except gax.GoogleAPICallError as e:
            errors.append(f"list bucket {bucket.name}: {e}")
            continue

        empty_failed = False
        for blob in blobs:
            try:
                blob.delete()
            except gax.NotFound:
                # A concurrently removed object does not remove the bucket;
                # continue emptying and still prove bucket deletion below.
                continue
            except gax.GoogleAPICallError as e:
                errors.append(f"empty bucket {bucket.name}: {e}")
                empty_failed = True
        if empty_failed:
            continue
        if delete_with_retry(bucket.delete, resource_desc=f"bucket {bucket.name}"):
            counters["cleaned"] += 1
        else:
            errors.append(f"delete bucket {bucket.name} failed")
    return errors


def _run_family_sweep(
    family: str,
    sweep: Callable[[], list[str]],
    cleanup_errors: list[str],
) -> None:
    """Run one independent resource-family sweep and retain every failure."""
    try:
        cleanup_errors.extend(sweep())
    except Exception as exc:
        cleanup_errors.append(f"{family} inventory failed: {exc}")


@handle_gcp_errors
def main() -> int:
    """Sweep leftover security test fixtures created by isvtest (dual-gate ownership)."""
    parser = argparse.ArgumentParser(description="Security test teardown (safety-net sweep)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    # Per-fixture created flags forwarded by the provider config as the strings
    # "true"/"false"; hints for which families a run actually populated.
    parser.add_argument("--kms-key-created", default="")
    parser.add_argument("--cmek-disk-created", default="")
    parser.add_argument("--cmek-grant-added", default="")
    parser.add_argument("--sa-created", default="")
    parser.add_argument("--lp-role-created", default="")
    parser.add_argument("--lp-binding-created", default="")
    parser.add_argument("--lp-sa-created", default="")
    parser.add_argument("--lp-bucket-created", default="")
    parser.add_argument("--ti-sa-created", default="")
    parser.add_argument("--ti-vpc-created", default="")
    parser.add_argument("--ti-kms-created", default="")
    parser.add_argument("--ti-bucket-created", default="")
    parser.add_argument("--ti-instance-created", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "teardown",
        "resources_cleaned": 0,
        "resources_skipped_unowned": 0,
    }

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        print(json.dumps(result, indent=2))
        return 0

    # The per-fixture created flags the provider config forwards are advisory
    # only. A standalone `--phase teardown` after a crash runs in a process where
    # the test steps never executed, so each flag renders to "false"; gating the
    # sweep on them would make that recovery path a silent no-op that cleans
    # nothing. Every family is therefore swept unconditionally below, and the
    # dual-gate ownership check (owned name scope plus resource-native
    # provenance) guards against touching another run's or the operator's
    # resources. The flags are recorded only as a hint of what a same-process
    # run reported creating.
    def _created(value: str) -> bool:
        return value == "true"

    reported_created = sorted(
        family
        for family, created in (
            ("kms_key", _created(args.kms_key_created) or _created(args.ti_kms_created)),
            ("cmek_disk", _created(args.cmek_disk_created)),
            ("cmek_service_agent_grant", _created(args.cmek_grant_added)),
            (
                "service_account",
                _created(args.sa_created) or _created(args.lp_sa_created) or _created(args.ti_sa_created),
            ),
            ("custom_role", _created(args.lp_role_created)),
            ("project_iam_binding", _created(args.lp_binding_created)),
            ("bucket", _created(args.lp_bucket_created) or _created(args.ti_bucket_created)),
            ("network", _created(args.ti_vpc_created)),
            ("instance", _created(args.ti_instance_created)),
        )
        if created
    )
    print(
        f"teardown: run reported creating {reported_created or 'no'} fixture families; "
        "sweeping all families by dual-gate ownership",
        file=sys.stderr,
    )

    counters: dict[str, int] = {"cleaned": 0, "skipped": 0}
    cleanup_errors: list[str] = []
    run_token = _run_token()

    # Fail closed when no run id is available. The run-id token is the mandatory
    # name-scope gate; resource-native provenance is the independent second gate.
    # With no token the dual-gate check can own nothing. A
    # standalone `--phase teardown` started in a fresh process (RUN_ID unset)
    # would otherwise finish as a success-looking no-op that silently leaves the
    # deliberately-preserved fixtures behind. Require the operator to re-export
    # the original run's RUN_ID (or LS_RUN_ID) so the sweep can actually own and
    # remove them (documented in docs/references/gcp.md).
    if not run_token:
        result["error"] = (
            "no run id available to prove fixture ownership: export RUN_ID (or "
            "LS_RUN_ID) set to the original run's id before a standalone "
            "`--phase teardown` sweep. Without it the dual-gate ownership check "
            "owns nothing and the sweep would be a success-looking no-op that "
            "leaves preserved fixtures behind."
        )
        print(json.dumps(result, indent=2))
        return 1

    try:
        project = resolve_project(args.project)
    except Exception as exc:
        result["error"] = str(exc)
    else:
        # Preserve dependency order while containing both enumeration and
        # deletion failures to one family. An unavailable KMS inventory, for
        # example, must not prevent independent compute, IAM, or bucket cleanup.
        family_sweeps: tuple[tuple[str, Callable[[], list[str]]], ...] = (
            ("disk", lambda: _sweep_disks(project, run_token, counters)),
            ("instance", lambda: _sweep_instances(project, run_token, counters)),
            ("network", lambda: _sweep_networks(project, args.region, run_token, counters)),
            # Remove key-use grants and destroy key material only after Compute
            # dependants are gone.
            ("kms_key", lambda: _sweep_kms_keys(project, run_token, counters)),
            (
                "project_iam_binding",
                lambda: _sweep_project_iam_bindings(project, run_token, counters),
            ),
            (
                "workload_identity_pool",
                lambda: _sweep_workload_identity_pools(project, run_token, counters),
            ),
            ("service_account", lambda: _sweep_service_accounts(project, run_token, counters)),
            ("custom_role", lambda: _sweep_custom_roles(project, run_token, counters)),
            ("bucket", lambda: _sweep_buckets(project, run_token, counters)),
        )
        for family, sweep in family_sweeps:
            _run_family_sweep(family, sweep, cleanup_errors)

    result["resources_cleaned"] = counters["cleaned"]
    result["resources_skipped_unowned"] = counters["skipped"]
    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors

    # Success when the sweep completed without an unrecoverable error: a clean
    # walk that found nothing to delete is a successful no-op.
    result["success"] = "error" not in result and not cleanup_errors
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
